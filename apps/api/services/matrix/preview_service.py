"""preview_service.py — design/31 cut 2b：预览比价的编排器。

一句话：**在沙箱里把官方那条链路整条跑一遍**（校对入库 → 对齐 → 矩阵），
拿到结果，然后把一切写入回滚掉。

## 为什么不另写一份"只读的近似比价"

因为那样就有两份"比价"实现了。CLAUDE.md 的"同一个业务结果"不是风格偏好：
预览说甲最低、正式说乙最低的那一天，没人能判断哪个是对的，两边都得重查。
跑同一条代码路径，预览与正式的差别就只剩输入（含不含未确认草稿），
这是可以解释的差别；实现差异不是。

## 边界

- **不落库**：由 `preview_sandbox` 保证，机制与已知漏洞见那个模块的文档。
- **不给正式结论**：`basis="preview"` 的结果在 `BidMatrixResult` 契约层就
  禁止 firm 推荐（design/31 cut 1），不靠这里自觉。
- **不替用户确认采购清单。** 没有已确认的 `TenderListSession` 时不会在
  沙箱里替用户把清单确认掉——那会让用户看到一份基于"系统替我确认的清单"
  的比价，而他并不知道自己确认过什么。采购清单确认是招标侧的一次点击，
  跟"逐行确认报价"不是一回事。

## design/32：没有采购清单时不再拒绝，退到报价派生轴

2026-08-22 之前，没有已确认 `TenderListSession` 就直接 `PreviewNotReady`。
用户指出这个前提太强："比价核心是货比三家……如果没有采购清单也应该可以
对齐"。design/32 §2 用真实语料验证了这个判断：同一项目里每家供应商报价的
行数、以及"数量"列逐位取值，跨供应商完全一致——报价是照着同一份清单写的，
位置本身携带对齐信息。

现在的顺序：先按 `get_current_confirmed_session` 找已确认清单；找不到就调
`quote_derived_axis.build_quote_derived_axis`，从已入库报价中挑条目最多的
一份当基准，其余按位置对齐、用数量序列校验（复用 `_sequential_matches`，
zero 新增对齐算法——那套判据本来就不要求 DN 存在）。两条路径产出的矩阵都
打上 `axis_kind`，好让调用方和界面知道这份结果的证据强度不一样：
`quote_derived` 的矩阵没有招标侧真值，只能说"这几家同一行报价不一样"，
不能说"某家漏报了招标要求的项目"。

`axis_kind='quote_derived'` **只能进预览**——契约层拦（见
`BidMatrixResult._quote_derived_axis_is_preview_only`），不靠这里自觉。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from apps.api.services.matrix.preview_sandbox import preview_sandbox

log = logging.getLogger(__name__)


class PreviewNotReady(Exception):
    """预览的前置条件不成立（例如采购清单尚未确认）。调用方转成 409。"""


@dataclass
class PreviewResult:
    matrix: dict[str, Any]
    """`build_anchor_matrix` 的产物，已打上 basis='preview'。"""
    unconfirmed_rows: int
    """有多少行掺了未确认数据。"""
    confirmed_submissions: list[int]
    """沙箱内建出来的 submission id —— **沙箱外这些 id 不存在**，只用于
    把矩阵列跟输入对上，不要拿去查库或回传给别的接口。"""
    notes: list[str]
    """跑的过程中值得让用户知道的事（某份文件入库时被质量门拦下之类）。"""
    queue: Any = None
    """确认队列（`preview_ordering.PreviewOrdering`），**在沙箱内构建**。

    必须在沙箱里建、连证据一起取出来：队列每一项都要指回"原文哪一页哪一行"
    以及那一行识别到了什么，而这些来自 `BidQuoteLine`——沙箱一退出就全部
    回滚，`bid_quote_line_id` 变成悬空的数字。事后再开接口去查是查不到的。
    """


def build_preview_matrix(
    project_id: int,
    category: str,
    confirmations: Sequence[Any],
) -> PreviewResult:
    """在沙箱里跑完整条链路，返回预览矩阵。

    Args:
        confirmations: 每份投标文件一个 `BatchConfirmRequest` 形状的对象——
            就是"确认入库"按钮会发的那个 body。预览与正式用同一份输入，
            差别只在这里不落库。
    """
    from apps.api.services.alignment.anchor_match import import_and_match
    from apps.api.services.matrix.bid_matrix import build_anchor_matrix
    from apps.api.services.submission.quote_confirmation_service import confirm_batch
    from apps.api.services.tender.tender_list import rebuild_anchors
    from apps.api.services.tender.tender_session_service import (
        get_current_confirmed_session,
    )

    notes: list[str] = []

    with preview_sandbox() as db:
        session = get_current_confirmed_session(db, project_id, category)
        anchors = rebuild_anchors(session) if session and session.anchors_json else None
        tender_list_session_id = session.id if anchors else None

        # ── 1) 入库：走真实 confirm_batch（dry_run=False）。沙箱负责回滚。
        # 这里**故意不**用 dry_run=True：那条分支自己就会 rollback，
        # 后面的对齐就读不到刚入库的报价，整条链路串不起来。
        submission_ids: list[int] = []
        for body in confirmations:
            try:
                # gates_advisory=True：质量门只警告不阻断。用户原话——「能不能
                # 比价是一个等级，有几个能比价是另外一个等级」。一家的声明总价
                # 对不上，不该让另外两家也看不成；而且这类失败**往往是我们自己
                # 的识别缺陷**（实测凯硕新正那次：合计行被当成第 90 条报价行，
                # 总额算了两遍），把用户挡在门外去修一个他制造不了的问题。
                # 官方路径的门一点没动（gates_advisory 默认 False）。
                out = confirm_batch(db, body, dry_run=False, gates_advisory=True)
            except Exception as exc:                      # noqa: BLE001
                # 门降级之后仍然抛出的，是"请求本身不成立"那一类（job 不存在、
                # 类型不对）。一份进不去不该让整个预览失败，但绝不静默跳过。
                notes.append(f"「{getattr(body, 'job_id', '?')}」未能进入预览：{exc}")
                log.info("preview: confirm_batch failed job=%s: %s",
                         getattr(body, "job_id", "?"), exc)
                continue
            sid = out.get("submission_id") if isinstance(out, dict) else None
            if sid:
                submission_ids.append(int(sid))
            # 进来了但带着疑点的，如实说——降级不是放行。
            for issue in (out.get("issues") or []) if isinstance(out, dict) else []:
                supplier = getattr(body, "supplier_name", None) or getattr(body, "job_id", "?")
                notes.append(
                    f"「{supplier}」已进入预览，但有疑点："
                    f"{issue.get('message') or issue.get('error')}")

        if not submission_ids:
            raise PreviewNotReady("没有任何报价能进入预览，无法比价。" + (
                f"原因：{notes[0]}" if notes else ""))

        # ── design/32：没有已确认采购清单时，从已入库报价自己派生行轴。
        axis_kind = "tender_anchor"
        if anchors is None:
            from apps.api.services.matrix.quote_derived_axis import (
                NoUsableQuoteRows, build_quote_derived_axis,
            )
            try:
                derived = build_quote_derived_axis(db, category, submission_ids)
            except NoUsableQuoteRows as exc:
                raise PreviewNotReady(
                    f"项目 {project_id} / 品类 {category} 尚无已确认采购清单，"
                    f"且{exc}") from exc
            anchors = derived.anchors
            axis_kind = "quote_derived"
            notes.append(derived.note)

        # ── 2) 对齐：同样是官方函数——无论行轴来自采购清单还是报价派生，
        # 走的都是同一条 import_and_match，见 quote_derived_axis.py 的模块文档。
        summary, _per_supplier = import_and_match(
            db, None, project_id, category,
            submission_ids=submission_ids,
            anchors=anchors,
            tender_list_session_id=tender_list_session_id,
        )

        # ── 3) 矩阵：官方函数，参数与 routes/analysis.py 的官方调用一致。
        matrix = build_anchor_matrix(
            db,
            anchors=anchors,
            tender_list_session_id=tender_list_session_id,
            used_submission_ids=submission_ids,
            supplier_ids=[],
            submission_ids=submission_ids,
            project_id=project_id,
            category=category,
            allowed_group_ids=None,
        )

        unconfirmed = _count_unconfirmed_rows(matrix)
        matrix["basis"] = "preview"
        matrix["preview_unconfirmed_rows"] = unconfirmed
        matrix["axis_kind"] = axis_kind
        # 预览不许带正式结论。契约层也会拦（cut 1），这里先降级是为了让
        # 拦截成为"不可能发生"而不是"发生了会报错"。
        if matrix.get("recommendation_level") == "firm":
            matrix["recommendation_level"] = "conditional"
            matrix.setdefault("recommendation_reasons", []).append(
                "本结果为预览口径（含未确认报价），不作为定标依据")
        if matrix.get("comprehensive_recommendation_status") == "firm":
            matrix["comprehensive_recommendation_status"] = "conditional"
        if getattr(summary, "pending", None):
            notes.append(f"对齐后仍有 {summary.pending} 行待人工判定")

        return PreviewResult(
            matrix=matrix,
            unconfirmed_rows=unconfirmed,
            confirmed_submissions=submission_ids,
            notes=notes,
            queue=build_confirmation_queue(matrix, db=db),
        )


def _count_unconfirmed_rows(matrix: dict[str, Any]) -> int:
    """预览里每一行都来自未确认草稿，所以这里数的是"有数据的行"。

    数 `rows` 总数会把招标清单里根本没人报价的空行也算进去，那些行不是
    "含未确认数据"，是"没有数据"——两者混在一起，这个数字就没法解释。
    """
    n = 0
    for row in matrix.get("rows") or []:
        # 键名以 build_anchor_matrix 的真实产物为准（`suppliers`/`price`/`total`），
        # 不是 unit_price/total_price——初版按后者写，永远数出 0。
        for c in row.get("suppliers") or []:
            if isinstance(c, dict) and (c.get("price") is not None
                                        or c.get("total") is not None):
                n += 1
                break
    return n


#: 真正需要人工确认的格子状态。**只有 pending**。
#:
#: 初版判据是"非 quoted 即待确认"，实测把队列灌成了 13 倍：quoted 169 /
#: missing 50 / aggregated 36 / pending 9，列出 95 条，只有 9 条人能动。
#:   - `missing`    供应商压根没报这一行。不是"去核对一下"，是"人家没报价"。
#:   - `aggregated` 合并行，有自己的审核路径，不是逐格确认。
#:   - `excluded`   已被排除，更不该回到待办里。
_CONFIRMABLE_CELL_STATUS = frozenset({"pending"})


def _cell_evidence(db, cell: dict[str, Any]) -> dict[str, Any] | None:
    """一条待确认格子的「原文依据」。

    design/32 §11：用户问「待确认怎么确认？去看纸质版本找到那一行？」——不该。
    识别产物每行都带 `source_ref`（实测形如 `{"page": 6, "table": 0, "row": 44}`），
    把它连同该行识别到的全部字段一起显示出来，用户一眼能看出是哪个字段没读到。

    这里给的是**「我们识别成了什么」**，不是原文影像。区别要在界面上说清楚：
    它足以判断"哪个字段空了"，不足以证明"原文写的就是这个"。
    """
    from apps.api.models.bid_submission import BidQuoteLine

    bql_id = cell.get("bid_quote_line_id")
    if not bql_id:
        return None
    bql = db.get(BidQuoteLine, bql_id)
    if bql is None:
        return None
    meta = bql.extraction_meta or {}
    src = (meta.get("source_ref") or {}) if isinstance(meta, dict) else {}
    return {
        "page": src.get("page"),
        # 跨页合并表的行只知道落在 page..page_end 之间，拆不到具体页（见
        # `paddle_vl._merged_page_spans`）。有这个键就说明页码是**区间不是断言**，
        # 界面必须照区间显示——把 page 单独摆出来会把用户送到错的一页。
        "page_end": src.get("page_end"),
        "row": src.get("row"),
        "raw_name": bql.raw_name,
        "standard_name": bql.standard_name,
        "spec": bql.spec,
        "unit": bql.unit,
        "qty": bql.qty,
        "unit_price": bql.unit_price,
        "unit_price_excl_tax": bql.unit_price_excl_tax,
        "total_price": bql.total_price,
        "tax_rate": bql.tax_rate,
        "pending_note": cell.get("pending_note"),
    }


def build_confirmation_queue(matrix: dict[str, Any], db=None):
    """矩阵 → 确认队列（cut 3 的 `build_ordering` 的适配层）。

    队列只装真正待人工确认的格子；"有没有可用价格"是另一件事，由
    `PreviewCell.unit_price` 单独表达（missing 同样没价，但它不是待办）。
    """
    from apps.api.services.matrix.preview_ordering import (
        PreviewCell, PreviewRow, build_ordering,
    )

    rows: list[PreviewRow] = []
    labels = {s["id"]: (s.get("name") or s.get("letter") or str(s["id"]))
              for s in (matrix.get("suppliers") or [])}
    #: (anchor_key, supplier_key) → 原始矩阵格子。队列项只带这两个键，
    #: 取证据时要靠它找回格子上的 bid_quote_line_id / pending_note。
    cell_index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in matrix.get("rows") or []:
        cells = []
        for c in row.get("suppliers") or []:
            if not isinstance(c, dict):
                continue
            status = c.get("cell_status")
            price = c.get("price")
            supplier_key = labels.get(c.get("id"), str(c.get("id")))
            anchor_key = str(row.get("anchor_seq") or row.get("material_name") or "?")
            cell_index[(anchor_key, supplier_key)] = c
            cells.append(PreviewCell(
                supplier_key=supplier_key,
                # 能当"同行报价"参与区间估算的，只有已确认且真有价的格子。
                unit_price=float(price) if status == "quoted" and price is not None else None,
                confirmable=status in _CONFIRMABLE_CELL_STATUS,
            ))
        rows.append(PreviewRow(
            anchor_key=str(row.get("anchor_seq") or row.get("material_name") or "?"),
            qty=row.get("quantity"),
            cells=tuple(cells),
        ))
    ordering = build_ordering(rows)

    # 给每条待确认附上原文依据。`db` 为 None（单元测试直接喂矩阵）时跳过——
    # 没有证据不影响排序，排序本来就只依赖价格和数量。
    if db is not None:
        ordering.evidence = {
            (imp.anchor_key, imp.supplier_key): ev
            for imp in ordering.queue
            if (ev := _cell_evidence(db, cell_index.get((imp.anchor_key, imp.supplier_key), {})))
        }
    return ordering
