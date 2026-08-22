"""quote_derived_axis.py — design/32 A1+A2：没有已确认采购清单时，从报价
本身派生比价的行轴。

## 为什么可以这么做（不是猜测，是量出来的）

design/32 §2 直接读结构化报价文件实测：同一项目里每家供应商的行数完全相同
（金桥 3 家各 91 行、徐汇 4 家各 138 行），且"数量"列逐位 100% 一致——报价
清单是照着同一份采购清单写的，位置本身就带着对齐信息。这不是这批语料碰巧
如此的猜测，是直接读文件量出来的。

## 这不是新的对齐算法

`_sequential_matches`（`anchor_match.py`）早就实现了"位置提议、DN 或数量
确认"这套判据——那是为「顺序直连」写的，而且**数量分支本来就不认 DN 是否
存在**（服务电缆/桥架这类没有 DN 口径的品类）。这里只是把它的输入换掉：
不喂 `TenderListSession` 解析出的锚点，喂**从某一家报价自己派生的锚点**。
对齐质量、逐行冲突隔离、pending 降级，全部沿用既有实现——零改动、零新增
回归面，也是 design/31 反复用过的"跑同一条代码路径"那条理由。

## A1：哪些行算「条目」

规则只有一条：`qty` 不是 None。这就是 `BidQuoteLine.qty` 这一列本来的含义
——识别/解析阶段已经把表头、空行、合计行滤在外，`qty=None` 就是"这一行
不是一条可比的物料"。不额外发明第二套判据。

## 边界（design/31 cut 1 同一个道理：写死在契约层，不是自觉）

这里产出的锚点**只能喂给预览**。`preview_service.py` 只在没有已确认
`TenderListSession` 时才调用本模块；官方 `/bid-matrix` 与导出从不调用。
`BidMatrixResult.axis_kind` 在契约层拦住 axis_kind=quote_derived 却
basis=official 的组合，双保险——不靠这里的调用方自觉。

## 这条轴天生比 TenderAnchor 弱，且这一点必须被看见

它来自某一家的报价，不是招标方的清单。能说"这几家同一行报价不一样"，
不能说"某家漏报了招标要求的项目"——没有任何东西记录了"应该有什么"。
调用方（`preview_service`）必须把这条限制原样透传给用户，不能只显示数字。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models.bid_submission import BidQuoteLine
from apps.api.services.ingestion.canonical import extract_valve_canonical
from apps.api.services.tender.tender_list import TenderAnchor


class NoUsableQuoteRows(Exception):
    """已确认报价里一行可用条目都没有——连派生锚点的原料都没有。"""


@dataclass
class QuoteDerivedAxis:
    anchors: list[TenderAnchor]
    reference_submission_id: int
    note: str
    #: 参与过挑选但因条目数不是最多而落选的候选，(submission_id, 条目数)。
    #: 只用于日志/诊断，不影响结果——但缺了它，"为什么选了这家不是那家"
    #: 就没法回答。
    candidates: list[tuple[int, int]] = field(default_factory=list)


def _document_order(rows: list[BidQuoteLine]) -> list[BidQuoteLine]:
    """跟 `anchor_match._doc_order` 同一套三态判据的单 submission 版本。

    那个函数要跨 submission 的 doc_index 输入，这里只服务"从一家报价自己
    派生锚点"这一件事，没有别的 submission 可比，直接内联更清楚。

    - 全部行有合法 document_row_index 且唯一连续 → 按它排序；
    - 否则（历史数据全缺 / 残缺）→ 回退入库(id)顺序（调用处已按 id 升序取出），
      不假装一份残缺的业务序号是完整的。
    """
    idx: list[tuple[BidQuoteLine, int | None]] = []
    for r in rows:
        meta = r.extraction_meta or {}
        dri = meta.get("document_row_index") if isinstance(meta, dict) else None
        idx.append((r, dri))
    present = [d for _, d in idx if d is not None]
    if present and len(present) == len(idx) and len(set(present)) == len(present):
        lo = min(present)
        if sorted(present) == list(range(lo, lo + len(present))):
            return [r for r, _ in sorted(idx, key=lambda x: x[1])]
    return rows


def build_quote_derived_axis(
    db: Session, category: str, submission_ids: list[int],
) -> QuoteDerivedAxis:
    """从已确认报价里挑一家做基准，把它的条目行变成锚点。

    基准挑选：条目行（A1 意义下）最多的那家；打平取 submission_id 最小的
    （最先确认的）。规则固定、可复现——同样的输入每次选出同一个基准，
    不随查询返回顺序漂移。
    """
    if not submission_ids:
        raise NoUsableQuoteRows("没有任何已确认报价，无法派生比价基准")

    stmt = (
        select(BidQuoteLine)
        .where(BidQuoteLine.submission_id.in_(submission_ids))
        .order_by(BidQuoteLine.submission_id.asc(), BidQuoteLine.id.asc())
    )
    if category:
        stmt = stmt.where(BidQuoteLine.category == category)
    rows = list(db.scalars(stmt).all())

    by_sub: dict[int, list[BidQuoteLine]] = {}
    for r in rows:
        by_sub.setdefault(r.submission_id, []).append(r)

    # A1：qty 是 None 就不是条目行。
    item_rows_by_sub = {
        sid: [r for r in rs if r.qty is not None] for sid, rs in by_sub.items()
    }
    non_empty = {sid: rs for sid, rs in item_rows_by_sub.items() if rs}
    if not non_empty:
        raise NoUsableQuoteRows(
            "已确认的报价里没有一行带有效数量，无法派生比价基准——"
            "这些行本身可能就有质量问题，建议先核对识别结果")

    ref_sub_id = min(non_empty, key=lambda sid: (-len(non_empty[sid]), sid))
    ref_rows = _document_order(non_empty[ref_sub_id])

    anchors: list[TenderAnchor] = []
    for i, r in enumerate(ref_rows, start=1):
        canon = r.canonical or extract_valve_canonical(r.standard_name or "", r.spec or "")
        anchors.append(TenderAnchor(
            seq=i,
            name=r.standard_name or r.raw_name or "",
            spec=r.spec or "",
            unit=r.unit or "",
            qty=r.qty,
            canonical=canon or {},
            source_ref={"quote_derived_from_submission_id": ref_sub_id, "quote_line_id": r.id},
        ))

    return QuoteDerivedAxis(
        anchors=anchors,
        reference_submission_id=ref_sub_id,
        note=(
            f"未提供采购清单，比价基准取自本轮报价中条目最多的一份"
            f"（{len(anchors)} 项）。这只能说明各家同一行报价是否不同，"
            f"不能判断是否有招标要求的项目被漏报——没有招标清单，就没有"
            f"「应该有什么」的依据。"
        ),
        candidates=sorted(
            ((sid, len(rs)) for sid, rs in non_empty.items()),
            key=lambda x: (-x[1], x[0]),
        ),
    )
