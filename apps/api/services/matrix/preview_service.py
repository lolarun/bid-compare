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
- **要求采购清单已确认**：没有已确认的 `TenderListSession` 时直接拒绝，
  **不**在沙箱里替用户把清单确认掉。技术上做得到，但那会让用户看到一份
  基于"系统替我确认的清单"的比价，而他并不知道自己确认过什么——预览可以
  模糊，基准不能。采购清单确认是招标侧的一次点击，跟"逐行确认报价"不是
  一回事，要求它已完成不违背这轮"先比价、别逐行确认"的初衷。
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
        if not session or not session.anchors_json:
            # 跟官方 /bid-matrix 同一条拒绝理由，措辞也保持一致——同一个前置
            # 条件在两个入口给出两种说法，用户会以为是两回事。
            raise PreviewNotReady(
                f"项目 {project_id} / 品类 {category} 尚无已确认采购清单"
                "（TenderListSession）。请先完成采购清单上传和确认步骤。"
            )
        anchors = rebuild_anchors(session)

        # ── 1) 入库：走真实 confirm_batch（dry_run=False）。沙箱负责回滚。
        # 这里**故意不**用 dry_run=True：那条分支自己就会 rollback，
        # 后面的对齐就读不到刚入库的报价，整条链路串不起来。
        submission_ids: list[int] = []
        for body in confirmations:
            try:
                out = confirm_batch(db, body, dry_run=False)
            except Exception as exc:                      # noqa: BLE001
                # 一份文件入不了库不该让整个预览失败——预览的价值正是"先看个
                # 大概"。但也绝不静默跳过：哪份缺席、为什么，如实列出来。
                notes.append(f"「{getattr(body, 'job_id', '?')}」未能进入预览：{exc}")
                log.info("preview: confirm_batch failed job=%s: %s",
                         getattr(body, "job_id", "?"), exc)
                continue
            sid = out.get("submission_id") if isinstance(out, dict) else None
            if sid:
                submission_ids.append(int(sid))

        if not submission_ids:
            raise PreviewNotReady("没有任何报价能进入预览，无法比价。" + (
                f"原因：{notes[0]}" if notes else ""))

        # ── 2) 对齐：同样是官方函数。
        summary, _per_supplier = import_and_match(
            db, None, project_id, category,
            submission_ids=submission_ids,
            anchors=anchors,
            tender_list_session_id=session.id,
        )

        # ── 3) 矩阵：官方函数，参数与 routes/analysis.py 的官方调用一致。
        matrix = build_anchor_matrix(
            db,
            anchors=anchors,
            tender_list_session_id=session.id,
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


def build_confirmation_queue(matrix: dict[str, Any]):
    """矩阵 → 确认队列（cut 3 的 `build_ordering` 的适配层）。

    只把"没有可用单价"的格子交给排序模块——已经是 quoted 的格子不需要人去
    确认，把它们也塞进队列，队列就退化成"全部 89 行"，跟没有队列一样。
    """
    from apps.api.services.matrix.preview_ordering import (
        PreviewCell, PreviewRow, build_ordering,
    )

    rows: list[PreviewRow] = []
    labels = {s["id"]: (s.get("name") or s.get("letter") or str(s["id"]))
              for s in (matrix.get("suppliers") or [])}
    for row in matrix.get("rows") or []:
        cells = []
        for c in row.get("suppliers") or []:
            if not isinstance(c, dict):
                continue
            usable = c.get("cell_status") == "quoted" and c.get("price") is not None
            cells.append(PreviewCell(
                supplier_key=labels.get(c.get("id"), str(c.get("id"))),
                unit_price=float(c["price"]) if usable else None,
            ))
        rows.append(PreviewRow(
            anchor_key=str(row.get("anchor_seq") or row.get("material_name") or "?"),
            qty=row.get("quantity"),
            cells=tuple(cells),
        ))
    return build_ordering(rows)
