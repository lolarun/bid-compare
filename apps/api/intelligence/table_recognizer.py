"""table_recognizer.py — 公共表格识别骨架（招标 + 报价共用）。

公共流程：
  render全页 → ocr_pages_with_roles → adapter.detect_pages
  → 逐页(build_llm_input → LLM → expected_rows检查 → thinking_retry → adaptive_tiling)
  → adapter.extract_meta
  → compute_quality(PASS/REVIEW/BLOCKED)
  → reconcile_vs_excel（可选）
  → ExtractionDraft

变化点由 RecognizeAdapter 提供：detect_pages / row_prompt / extract_meta / name_key。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from apps.api.core.enums import RT_INVALID, RT_GRAND_TOTAL, RT_SUBTOTAL
from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
from apps.api.intelligence.extraction_draft import (
    ExtractionDraft, DraftRow, PageMetric, SourceRef,
    compute_quality, _EXPECTED_ROWS_MIN_RATIO,
)

log = logging.getLogger(__name__)

from apps.api.intelligence.pipeline import PAGE_CONCURRENCY  # single env source


# ─── Adapter 契约 ─────────────────────────────────────────────────────────────

@dataclass
class RecognizeAdapter:
    """两侧 adapter 的最小契约。"""
    doc_type: str                                           # "tender" | "quote"
    detect_pages: Callable[[list[str]], list[int]]          # htmls → target page numbers (1-based)
    row_prompt: str                                         # Stage-2 抽取 prompt（HTML 输入时用）
    name_key: str = "name"                                  # LLM 输出里的名称字段（招标=name, 报价=material）
    extract_meta: Callable | None = None                    # 可选：从非目标页提取 meta
    # 可选：若不同输入格式需要不同 prompt，则提供此 callable；
    # 入参 input_mode ("table_grid"|"html_fallback"|"tiled")，返回 prompt 字符串
    prompt_for_mode: Callable[[str], str] | None = None


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def _needs_review(c, cls: list, i: int, table_roles: set) -> bool:
    """Flash→Plus 升级条件（§四，任一满足即复判）。

    高置信不跳过 Plus，当存在以下文档级结构冲突时强制升级：
    - 预测 header 但下一页不是 continuation（孤立 header → 可能误判）
    - 预测 subtotal 但前一页是 continuation（链末尾误判 → 用户要求）
    - 前后均为 continuation 而当前非表格（链中断 → 角色跳变）
    - orientation 与相邻表格页不一致（旋转方向冲突）
    """
    from apps.api.intelligence.page_classifier import VisualPageRole, OCR_SKIP_ROLES

    # ── 已有条件 ────────────────────────────────────────────────────────────
    if c.role == VisualPageRole.UNKNOWN:
        return True
    if c.confidence < 0.85:
        return True
    # contains_table 与 role 矛盾
    if (not c.contains_table) and c.role in table_roles:
        return True
    if c.contains_table and c.role in (OCR_SKIP_ROLES - {VisualPageRole.COVER,
                                                         VisualPageRole.BID_LETTER,
                                                         VisualPageRole.OTHER}):
        return True
    # 续表关系不明：判为 continuation 但前一页不是表格页
    cont_roles = {VisualPageRole.QUOTE_TABLE_CONTINUATION,
                  VisualPageRole.TENDER_TABLE_CONTINUATION}
    if c.role in cont_roles:
        prev = cls[i - 1].role if i > 0 else None
        if prev not in table_roles:
            return True
    if c.mixed_content:
        return True

    # ── 新增：文档级结构一致性冲突 ──────────────────────────────────────────
    header_roles = {VisualPageRole.QUOTE_TABLE_HEADER, VisualPageRole.TENDER_TABLE_HEADER}
    prev_role = cls[i - 1].role if i > 0 else None
    next_role = cls[i + 1].role if i + 1 < len(cls) else None

    # §3-a 预测 header 但紧随页不是 continuation/header（孤立或链断裂）
    if c.role in header_roles:
        if next_role is not None and next_role not in cont_roles and next_role not in header_roles:
            return True

    # §2 续表链末尾出现 subtotal，且 has_line_items 语义未知 → Plus 复判
    # 若 flash 已明确 has_line_items=True/False，由语义覆写层处理，无需 Plus
    if c.role == VisualPageRole.SUBTOTAL_OR_SUMMARY and prev_role in cont_roles:
        if c.has_line_items is None:
            return True

    # §3-c 前后均为 continuation 而当前非表格 → 链中断/角色跳变
    if c.role not in table_roles and prev_role in cont_roles and next_role in cont_roles:
        return True

    # §3-e orientation 与相邻表格页不一致
    if c.role in table_roles and c.orientation is not None:
        neighbor_orients = []
        if i > 0 and cls[i - 1].role in table_roles and cls[i - 1].orientation:
            neighbor_orients.append(cls[i - 1].orientation)
        if i + 1 < len(cls) and cls[i + 1].role in table_roles and cls[i + 1].orientation:
            neighbor_orients.append(cls[i + 1].orientation)
        if neighbor_orients and all(o != c.orientation for o in neighbor_orients):
            return True

    return False


def _remap_for_doc_type(c: Any, doc_type: str) -> Any:
    """将 quote_table_* ↔ tender_table_* 按 doc_type 纠偏。
    模型在招标文件中可能将采购清单误判为 quote_table_*（反之亦然）；此函数在
    flash 和 plus 结果写入 cls[] 后立即调用，保证续表关系检查拿到正确角色。
    """
    from apps.api.intelligence.page_classifier import VisualPageRole, VisualPageClassification
    _q2t = {
        VisualPageRole.QUOTE_TABLE_HEADER: VisualPageRole.TENDER_TABLE_HEADER,
        VisualPageRole.QUOTE_TABLE_CONTINUATION: VisualPageRole.TENDER_TABLE_CONTINUATION,
    }
    _t2q = {v: k for k, v in _q2t.items()}
    mapping = _q2t if doc_type == "tender" else _t2q
    if c.role not in mapping:
        return c
    new_role = mapping[c.role]
    return VisualPageClassification(
        page=c.page, role=new_role, confidence=c.confidence,
        contains_table=c.contains_table, orientation=c.orientation,
        continues_from_page=c.continues_from_page, mixed_content=c.mixed_content,
        evidence=c.evidence, source=c.source,
        has_line_items=c.has_line_items,
        estimated_line_item_count=c.estimated_line_item_count,
        has_column_header=c.has_column_header,
        has_total_row=c.has_total_row,
        table_structure_continues=c.table_structure_continues,
    )


def _classify_pages(
    provider: Any, thumbnails: list[bytes],
    doc_type: str, notify=None, _debug: dict | None = None,
    file_path: str | None = None,
    render_full: Any = None,
) -> tuple[list, int, int]:
    """视觉页面分类：flash 批量 → Plus 复判 → 语义覆写（三阶段）。

    三阶段结构：
      Phase 1 (Flash)   → flash_cls（Layer 0）
      Phase 2 (Plus)    → after_plus_cls（Layer 1）：仅做 Plus 复判，不做覆写
      Phase 3 (Semantic)→ final_cls（Layer 2）：用 has_line_items 做确定性覆写

    _debug 非 None 时填入三阶段快照，供混淆矩阵分层统计使用。
    """
    from apps.api.intelligence.page_classifier import (
        VisualPageClassification, VisualPageRole,
        QUOTE_TARGET_ROLES, TENDER_TARGET_ROLES,
    )
    table_roles = TENDER_TARGET_ROLES if doc_type == "tender" else QUOTE_TARGET_ROLES
    cont_roles = {VisualPageRole.QUOTE_TABLE_CONTINUATION, VisualPageRole.TENDER_TABLE_CONTINUATION}
    try:
        from apps.api.intelligence.providers.dashscope_ocr import (
            _VISUAL_FLASH_MODEL as _FLASH_MDL,
            _VISUAL_PLUS_MODEL as _PLUS_MDL,
            _VISUAL_PROMPT_VERSION as _PROMPT_VER,
        )
    except ImportError:
        _FLASH_MDL = _PLUS_MDL = None
        _PROMPT_VER = "v4"

    # ── Phase 1: Flash 批量分类 ──────────────────────────────────────────────
    flash, _failures = provider.classify_pages_visual(
        thumbnails, doc_type,
        model=_FLASH_MDL,
        prompt_version=_PROMPT_VER,
        file_path=file_path,
    )
    cls = [_remap_for_doc_type(VisualPageClassification.from_dict(d), doc_type) for d in flash]
    flash_cls = list(cls)  # Layer 0 快照

    # ── Phase 2: Plus 复判（B档并发 + 收敛检查）──────────────────────────────
    # 策略：用 Flash 快照的 chain_ctx 并发发出全部 Plus 请求；若某页被 Plus 改变了
    # (role, orientation)，则对其后方所有复判页顺序重跑（通常 0 次额外调用）。
    plus_count = 0
    review_indices = [i for i, c in enumerate(cls) if _needs_review(c, cls, i, table_roles)]

    if review_indices:
        flash_snap = list(cls)  # Flash 快照；并发期间不可变

        # pypdfium2 PdfDocument 跨线程不安全；在主线程统一预渲染高清图再并发发 API。
        page_imgs: dict[int, Any] = {
            idx: (render_full(flash_snap[idx].page) if render_full is not None else None)
            for idx in review_indices
        }

        def _run_plus(idx: int):
            c = flash_snap[idx]
            neighbors = [thumbnails[j] for j in (idx - 1, idx + 1) if 0 <= j < len(thumbnails)]
            chain_ctx = [
                {"page": p.page, "role": p.role.value, "orientation": p.orientation}
                for p in flash_snap[:idx] if p.role in table_roles
            ]
            reviewed = provider.review_pages_visual(
                page_imgs[idx], neighbors, flash[idx], c.page,
                chain_context=chain_ctx, model=_PLUS_MDL)
            return _remap_for_doc_type(VisualPageClassification.from_dict(reviewed), doc_type)

        with ThreadPoolExecutor(max_workers=len(review_indices)) as ex:
            para_results: dict[int, Any] = dict(
                zip(review_indices, ex.map(_run_plus, review_indices))
            )

        para_cls = list(cls)
        for idx, result in para_results.items():
            para_cls[idx] = result

        # 收敛检查：找出 Plus 改变了 (role, orientation) 的页
        flipped = [
            idx for idx in review_indices
            if (para_results[idx].role, para_results[idx].orientation)
            != (flash_snap[idx].role, flash_snap[idx].orientation)
        ]

        cls = para_cls
        plus_count = len(review_indices)

        if flipped:
            # 有翻转：对最早翻转页之后的所有复判页顺序重跑，使用已更新的 cls 作 chain_ctx
            earliest_flip = min(flipped)
            affected = [idx for idx in review_indices if idx > earliest_flip]
            log.info(
                "plus-parallel convergence: %d flipped=%s, sequential retry %d pages",
                len(flipped), flipped, len(affected),
            )
            for idx in affected:
                c = cls[idx]
                neighbors = [thumbnails[j] for j in (idx - 1, idx + 1) if 0 <= j < len(thumbnails)]
                chain_ctx = [
                    {"page": p.page, "role": p.role.value, "orientation": p.orientation}
                    for p in cls[:idx] if p.role in table_roles
                ]
                page_img = render_full(c.page) if render_full is not None else None
                reviewed = provider.review_pages_visual(
                    page_img, neighbors, flash[idx], c.page,
                    chain_context=chain_ctx, model=_PLUS_MDL)
                cls[idx] = _remap_for_doc_type(VisualPageClassification.from_dict(reviewed), doc_type)
            plus_count += len(affected)

    after_plus_cls = list(cls)  # Layer 1 快照

    # ── Phase 3: 语义覆写（has_line_items 确定性判定，不依赖链长度）──────────
    cont_type = (VisualPageRole.QUOTE_TABLE_CONTINUATION if doc_type == "quote"
                 else VisualPageRole.TENDER_TABLE_CONTINUATION)
    for i, c in enumerate(cls):
        if c.role == VisualPageRole.SUBTOTAL_OR_SUMMARY and c.has_line_items is True:
            cls[i] = VisualPageClassification(
                page=c.page, role=cont_type,
                confidence=c.confidence, contains_table=True,
                orientation=c.orientation,
                continues_from_page=c.continues_from_page,
                mixed_content=False,
                has_line_items=True,
                estimated_line_item_count=c.estimated_line_item_count,
                has_column_header=c.has_column_header,
                has_total_row=c.has_total_row,
                table_structure_continues=c.table_structure_continues,
                evidence=list(c.evidence or []) + ["§2-semantic: has_line_items=true → continuation"],
                source=c.source,
            )
            log.info("p%d semantic override: subtotal→%s (has_line_items=True)", c.page, cont_type.value)

    if _debug is not None:
        _debug["flash"] = flash_cls
        _debug["after_plus"] = after_plus_cls
        _debug["final"] = list(cls)

    if notify:
        notify(f"视觉分类完成（flash {len(cls)} 页，plus 复判 {plus_count} 页）", 18)
    return cls, len(cls), plus_count


_TAIL_RECALL_MIN_CHAIN = 3      # 连续目标页 ≥ 此值才视为「真报价链」，召回其紧邻尾页


def _tail_recall_pages(
    tgt: list[int], handled: set[int], total_pages: int,
    min_chain: int = _TAIL_RECALL_MIN_CHAIN,
) -> list[int]:
    """长报价链尾部紧邻页召回（页级召回，不依赖被误判页自身的视觉信号）。

    背景：侧向 90° 的报价末页常被视觉模型高置信误判为 bid_letter/certificate，
    且其 contains_table/has_line_items/continues_from_page 信号全为 False/None——
    靠信号无法召回。改用**位置**召回：一段连续目标页 ≥ min_chain 页时视为真报价链，
    其紧邻的下一页（若存在且尚未被处理）纳入候选目标页。

    安全性：真信函/证书页经 Stage-2 LLM 自然返回 {items:[]}，无害；漏判的报价尾页
    得以进入 OCR/抽取被召回。仅向尾扩展一页、不级联；不硬编码页码或供应商。

    Args:
        tgt: 已排序的视觉路由目标页（1-based）。
        handled: 已在 tgt 中处理的页集合，避免对已确认目标页重复召回。
                 不包含 meta_extra：被误判为 summary 的尾部页应允许被召回（LLM 兜底）。
        total_pages: 文档总页数。
    Returns:
        需追加为目标页的召回页列表（已排序去重）。
    """
    if not tgt:
        return []
    runs: list[list[int]] = []
    run = [tgt[0]]
    for p in tgt[1:]:
        if p == run[-1] + 1:
            run.append(p)
        else:
            runs.append(run)
            run = [p]
    runs.append(run)

    recall: set[int] = set()
    for r in runs:
        if len(r) < min_chain:
            continue
        nxt = r[-1] + 1
        if 1 <= nxt <= total_pages and nxt not in handled:
            recall.add(nxt)
    return sorted(recall)


_RECALL_PRICE_FIELDS = (
    "unit_price_incl_tax", "unit_price_excl_tax", "unit_price",
    "total_price_incl_tax", "total_price_excl_tax", "total_price",
)


def _filter_recall_rows(
    recall_rows: list[DraftRow],
    trusted_rows: list[DraftRow],
    name_key: str,
) -> tuple[list[DraftRow], list[DraftRow]]:
    """Apply merge gate to recall-page rows.

    Returns (accepted, review_candidates):
    - accepted: rows passing ALL criteria → caller merges into official rows.
    - review_candidates: rows failing ANY criterion, tagged with
      "recall_review_candidate" + reason codes → caller stores in
      draft.review_candidates (NOT in official rows / NOT into 比价 / NOT 入库).

    Rows are never silently dropped: every input row lands in exactly one bucket.

    Merge criteria (all required):
    1. source_ref.page > 0
    2. non-empty name (name_key or "material" or "name")
    3. qty is not None
    4. at least one price / total field is not None
    5. no qty_arithmetic_mismatch flag
    6. seq (if present) strictly > chain-tail seq and not duplicate within recall batch

    Rows without seq are not blocked on criterion 6 — they still pass if 1-5 hold.
    """
    trusted_seqs = [
        int(str(r.fields.get("seq")).strip())
        for r in trusted_rows
        if r.row_type == "quote_line"
        and str(r.fields.get("seq") or "").strip().isdigit()
    ]
    chain_tail_seq: int | None = max(trusted_seqs) if trusted_seqs else None

    seen_seqs: set[int] = set()
    accepted: list[DraftRow] = []
    review_candidates: list[DraftRow] = []
    for row in recall_rows:
        f = row.fields
        reasons: list[str] = []

        if not row.source_ref or row.source_ref.page <= 0:
            reasons.append("no_source_ref")

        name_val = str(f.get(name_key) or f.get("name") or f.get("material") or "").strip()
        if not name_val:
            reasons.append("no_name")

        if f.get("qty") is None:
            reasons.append("no_qty")

        if not any(f.get(k) is not None for k in _RECALL_PRICE_FIELDS):
            reasons.append("no_price")

        if "qty_arithmetic_mismatch" in row.validation_flags:
            reasons.append("arith_mismatch")

        seq_str = str(f.get("seq") or "").strip()
        if seq_str.isdigit():
            seq = int(seq_str)
            if chain_tail_seq is not None and seq <= chain_tail_seq:
                reasons.append(f"seq_overlap_chain_tail={chain_tail_seq}")
            if seq in seen_seqs:
                reasons.append(f"seq_dup={seq}")
            seen_seqs.add(seq)

        if reasons:
            row.validation_flags.append("recall_review_candidate")
            row.validation_flags.extend(reasons)
            review_candidates.append(row)
        else:
            accepted.append(row)
    return accepted, review_candidates


def recognize_tables(
    file_path: str,
    provider: Any,
    adapter: RecognizeAdapter,
    progress_cb: Callable[[str, int], None] | None = None,
    xlsx_path: str | None = None,
    target_pages: list[int] | None = None,      # 外部强制指定，否则视觉分类路由
) -> ExtractionDraft:
    """公共识别骨架，返回 ExtractionDraft（未确认）。

    必须调用者：招标侧 extract_bidlist、报价侧 extract_quote（均保持各自 API 不变）。
    """
    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, max(0, min(100, pct)))

    from apps.api.intelligence.page_classifier import (
        QUOTE_TARGET_ROLES, TENDER_TARGET_ROLES, META_ROLES, VisualPageRole,
    )

    # ── 懒渲染（先分类，再只高清渲染目标页）：缩略图优先 → 分类 → 算需高清页集 ──
    # 旧实现先把全部页渲成 2400px 高清图再分类，浪费内存（泰科龙 53 页峰值 1.6GB）。
    # 现在分类只用缩略图；全分辨率仅渲染 OCR/方向/Plus 真正需要的 ~12 页。
    _notify("视觉页面分类", 12)
    actual_page_count = DocumentLoader.get_page_count(file_path)
    thumbnails = DocumentLoader.to_thumbnails(file_path, max_pages=MAX_PAGES_UNLIMITED)
    rendered_pages = len(thumbnails)
    truncated = rendered_pages < actual_page_count
    total_pages = actual_page_count   # 真实页数，截断时 > rendered_pages

    def _render_full(pno: int) -> bytes:
        """按需渲染单页全分辨率（字节与旧 to_images()[pno-1] 一致）。"""
        return DocumentLoader.render_pages(file_path, [pno])[pno]

    page_cls, flash_pages, plus_pages = _classify_pages(
        provider, thumbnails, adapter.doc_type, notify=_notify,
        file_path=str(file_path), render_full=_render_full)
    role_by_page = {c.page: c for c in page_cls}

    table_roles = TENDER_TARGET_ROLES if adapter.doc_type == "tender" else QUOTE_TARGET_ROLES

    # ── 路由（§五）：仅表格页进入抽取；summary/brand/cover 仅供 meta ──────
    meta_extra = sorted(
        c.page for c in page_cls
        if c.role in META_ROLES or c.role == VisualPageRole.COVER
    )
    # recall_pages：长报价链尾部被高置信误判（如侧向90°末页判 bid_letter）召回的页。
    # 与 tgt（置信目标页）严格分离 —— 不进旋转检测输入、不进质量门 target、抽取失败
    # 不 BLOCK 整档（best-effort）。这样置信页的旋转/抽取行为完全不受召回扰动。
    # 注意：召回页要真正吐出尾行，依赖 Stage 2 给其逐页方向纠正；本阶段仅落地隔离的召回管线。
    recall_pages: list[int] = []
    if target_pages:
        tgt = sorted(target_pages)
    else:
        tgt = sorted(c.page for c in page_cls if c.role in table_roles)
        recall_pages = _tail_recall_pages(tgt, set(tgt), total_pages)
        if recall_pages:
            log.info("tail-recall (best-effort, isolated from rotation): %s", recall_pages)
    extract_pages = sorted(set(tgt) | set(recall_pages))   # 实际 OCR+抽取的页（含召回）
    recall_set = set(recall_pages)
    page_rotations = {c.page: c.orientation for c in page_cls if c.orientation}

    if not tgt:
        raise RuntimeError(
            f"No target table pages found in {Path(file_path).name} via visual "
            "classification. Check page roles or supply target_pages."
        )

    log.info("recognize_tables[%s]: file=%s total=%d target=%s rotated=%s",
             adapter.doc_type, Path(file_path).name, total_pages, tgt,
             {p: r for p, r in page_rotations.items() if p in tgt})

    # ── 仅对目标页(含召回) + meta页 跑 table_parsing OCR（按分类 orientation 转正）──
    ocr_pages = sorted(set(extract_pages) | set(meta_extra))
    # 懒渲染：现在才高清渲染真正需要的页 = OCR页 ∪ 方向探测样本页（未预知旋转的目标页）。
    # _detect_chain_orientation 探测样本取自 tgt（无预知旋转者），故 orient_sample 取其超集。
    orient_sample_pages = [p for p in tgt if not page_rotations.get(p)]
    needed_full = sorted(set(ocr_pages) | set(orient_sample_pages))
    page_imgs: dict[int, bytes] = DocumentLoader.render_pages(file_path, needed_full)
    _notify(f"OCR {len(ocr_pages)} 个表格/汇总页", 20)
    ocr_imgs = []
    for p in ocr_pages:
        img = page_imgs[p]
        deg = page_rotations.get(p, 0)
        if deg:
            img = _rotate_png_bytes(img, deg)
            page_imgs[p] = img        # 让后续 tiling 用转正后的图
        ocr_imgs.append(img)
    if ocr_imgs:
        ocr_res, ocr_failures = provider.ocr_pages_with_roles(ocr_imgs)
    else:
        ocr_res, ocr_failures = [], []
    html_by_page = {p: ocr_res[i][1] for i, p in enumerate(ocr_pages) if i < len(ocr_res)}
    page_htmls = [html_by_page.get(p, "") for p in range(1, total_pages + 1)]

    # ── 方向纠正回退（视觉分类 orientation=0 但 OCR 质量可能仍需转正）────────
    # 对视觉未标旋转的目标页调用算法探测兜底：若探测到候选角度则逐页修正
    # page_htmls / images，保证 _process_page 拿到转正后的 OCR HTML 和图像。
    # 按【连续表链】逐链确定方向，续页继承——不依赖波动的目标页集合（见 §11/根因层）。
    no_rot_tgt = [p for p in tgt if not page_rotations.get(p)]
    chain_orient: dict[int, int] = {}   # page -> 所属链方向角（供召回页继承）
    for _chain in _contiguous_runs(no_rot_tgt):
        _angle, _probe_cache = _detect_chain_orientation(
            _chain, page_htmls, page_imgs, provider, adapter.doc_type)
        if not _angle:
            continue
        log.info("chain %s-%s orientation=%d° (direct apply, probe cache %d pages)",
                 _chain[0], _chain[-1], _angle, len(_probe_cache))

        # 收集需要 re-OCR 的非 sample 页（批量提交，provider 内部 ThreadPoolExecutor 并发）
        _to_re_ocr: list[tuple[int, bytes]] = []
        for _p in _chain:
            chain_orient[_p] = _angle
            if _p in _probe_cache:
                _new_html, _new_img = _probe_cache[_p]
                page_htmls[_p - 1] = _new_html
                page_imgs[_p] = _new_img
                page_rotations[_p] = _angle
                log.info("  p%d reuse probe OCR → %d° (no re-OCR)", _p, _angle)
            else:
                _rot_img = _rotate_png_bytes(page_imgs[_p], _angle)
                _to_re_ocr.append((_p, _rot_img))

        # 批量 re-OCR：provider.ocr_pages_with_roles 内部已用 ThreadPoolExecutor 并发，
        # 受 per-key Semaphore 限流，不会触发 429
        if _to_re_ocr:
            _re_pages = [p for p, _ in _to_re_ocr]
            _re_imgs = [img for _, img in _to_re_ocr]
            try:
                _re_results, _re_failures = provider.ocr_pages_with_roles(_re_imgs)
            except Exception as _exc:
                log.warning("chain orient batch OCR failed for pages %s deg %d: %s",
                            _re_pages, _angle, _exc)
                _re_results, _re_failures = [], []

            for _idx, _p in enumerate(_re_pages):
                _rot_img = _re_imgs[_idx]
                if _idx < len(_re_results) and _re_results[_idx][1]:
                    page_htmls[_p - 1] = _re_results[_idx][1]
                    page_imgs[_p] = _rot_img
                    page_rotations[_p] = _angle
                    log.info("  p%d corrected → %d° (chain batch)", _p, _angle)
                else:
                    log.info("  p%d rotation NOT applied (OCR failed, kept original)", _p)

    # 召回页方向继承：继承紧邻前序页的链方向并直接 OCR 一次，不再二次投票。
    for _p in recall_pages:
        _inh = page_rotations.get(_p - 1) or chain_orient.get(_p - 1)
        if not _inh:
            continue
        _rot_img = _rotate_png_bytes(page_imgs[_p], _inh)
        try:
            _res, _ = provider.ocr_pages_with_roles([_rot_img])
            _ok = bool(_res and _res[0][1])
            _new_html = _res[0][1] if _ok else page_htmls[_p - 1]
            _new_img = _rot_img if _ok else page_imgs[_p]
        except Exception as _exc:
            log.warning("recall orient direct OCR failed p%d: %s", _p, _exc)
            _new_html, _new_img, _ok = page_htmls[_p - 1], page_imgs[_p], False
        if _ok:
            page_htmls[_p - 1] = _new_html
            page_imgs[_p] = _new_img
            page_rotations[_p] = _inh   # 仅成功才记审计
            log.info("  recall p%d direct → %d° (chain-inherited, no re-vote)", _p, _inh)
        else:
            log.info("  recall p%d rotation NOT applied (OCR failed, kept original)", _p)

    ocr_failed = len(ocr_failures)
    ocr_success = len(ocr_pages) - ocr_failed
    ocr_failed_indices = sorted(
        ocr_pages[f.get("pdf_page") - 1]
        for f in ocr_failures
        if f.get("pdf_page") and 1 <= f.get("pdf_page") <= len(ocr_pages)
    )

    # ── 跨页表头继承预扫（顺序，无 API 调用）────────────────────────────────
    # 对 tgt 中每一页解析 HTML，记录最近一次成功的 col header list。
    # 只对视觉分类为 *_continuation 角色的页面传递 inherited_header，
    # 确保有自有表头的 header/新起页面行为不变（防止 snapshot cache miss）。
    inherited_header_by_page: dict[int, list[str] | None] = {}
    _running_header: list[str] | None = None
    from apps.api.intelligence.table_parser import html_to_table_grids as _parse_grids
    for _p in sorted(tgt):
        try:
            _g = _parse_grids(page_htmls[_p - 1], _p)   # no hint — only detect own header
            if _g:
                _best = max(_g, key=lambda g: len(g.col_map))
                if _best.header and len(_best.header) >= 3:
                    _running_header = _best.header
        except Exception:
            pass
        # Only propagate to continuation pages (visual role contains "continuation").
        # Header/cover/other pages retain their own extraction behavior unchanged.
        _role_obj = role_by_page.get(_p)
        _role_val = str(getattr(getattr(_role_obj, "role", None), "value", "") or "")
        _is_continuation = "continuation" in _role_val.lower()
        inherited_header_by_page[_p] = _running_header if _is_continuation else None

    # ── 逐页提取（并发） ─────────────────────────────────────────────────
    all_rows: list[DraftRow] = []
    page_metrics: list[PageMetric] = []

    workers = min(PAGE_CONCURRENCY, len(extract_pages))
    total_tgt = len(extract_pages)
    completed = 0

    _notify(f"识别{total_tgt}个目标页", 20)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_page,
                page_no, page_no - 1,  # page_no is 1-based; idx 0-based
                page_htmls[page_no - 1],
                page_imgs[page_no],
                role_by_page.get(page_no),
                provider,
                adapter,
                page_rotations.get(page_no, 0),
                inherited_header_by_page.get(page_no),   # cross-page header inheritance
            ): page_no
            for page_no in extract_pages
        }
        results_by_page: dict[int, tuple] = {}
        failed_target: list[int] = []
        for future in as_completed(futures):
            page_no = futures[future]
            try:
                rows, metric = future.result()
                results_by_page[page_no] = (rows, metric)
            except Exception as exc:
                # 召回页 best-effort：失败不计入 failed_target、不 BLOCK 整档
                level = "info" if page_no in recall_set else "warning"
                getattr(log, level)("recognize_tables: page %d failed%s: %s",
                                    page_no, " (recall, best-effort)" if page_no in recall_set else "", exc)
                results_by_page[page_no] = ([], _empty_metric(page_no, page_no - 1, str(exc)))
                if page_no not in recall_set:
                    failed_target.append(page_no)
            completed += 1
            pct = 20 + int(completed / total_tgt * 60)
            _notify(f"已完成 {completed}/{total_tgt} 页", pct)

    # ── 含税字段重试（tax-field retry，最多一次/页）────────────────────────────
    # 触发条件（任一满足）：
    #   A. 字段覆盖率：quote_line 不含税价格已有，但含税单价/含税合价全部为 null
    #   B. 税额恒等式：tax_amount ≠ total_price_excl_tax × tax_rate （列识别错误特征）
    # 只对目标页（非召回页）触发；禁止默认 Best-of-N，仅在上述条件满足时触发一次。
    # 两个候选按「税额恒等式通过数 > 含税字段覆盖数」选优；相同时保留原始结果。
    _TAX_IDENTITY_TOL = 0.05    # 5% 容差（含入舍出）
    _TAX_IDENTITY_FAIL_RATE = 0.3  # 超过 30% 行失败则触发重试

    def _page_tax_quality(rows: list) -> tuple[int, int]:
        """返回 (tax_identity_ok_count, incl_field_count)，越高越优先。"""
        qlines = [r for r in rows if r.row_type == "quote_line"]
        incl_count = sum(
            1 for r in qlines
            if r.fields.get("unit_price_incl_tax") is not None
            or r.fields.get("total_price_incl_tax") is not None
        )
        identity_ok = 0
        identity_checked = 0
        for r in qlines:
            try:
                tp_excl = r.fields.get("total_price_excl_tax")
                tax_rt = r.fields.get("tax_rate")
                tax_amt = r.fields.get("tax_amount")
                if tp_excl is not None and tax_rt is not None and tax_amt is not None:
                    expected = float(tp_excl) * float(tax_rt)
                    if expected > 0:
                        identity_checked += 1
                        if abs(float(tax_amt) - expected) / max(expected, 1) < _TAX_IDENTITY_TOL:
                            identity_ok += 1
            except (TypeError, ValueError):
                pass
        return (identity_ok, incl_count)

    _target_only_pages = set(tgt) - recall_set
    _pages_needing_tax_retry: list[int] = []
    for _pno in sorted(_target_only_pages):
        _page_rows_0 = results_by_page.get(_pno, ([], None))[0]
        _page_qlines = [r for r in _page_rows_0 if r.row_type == "quote_line"]
        if not _page_qlines:
            continue
        _trigger = False
        # A. 字段覆盖率触发
        _excl_ok = sum(
            1 for r in _page_qlines
            if r.fields.get("unit_price_excl_tax") is not None
            or r.fields.get("total_price_excl_tax") is not None
        )
        _incl_ok = sum(
            1 for r in _page_qlines
            if r.fields.get("unit_price_incl_tax") is not None
            or r.fields.get("total_price_incl_tax") is not None
        )
        if _excl_ok >= len(_page_qlines) * 0.5 and _incl_ok == 0:
            _trigger = True
        # B. 税额恒等式触发（tax_amount ≠ total_excl × tax_rate → 列错位信号）
        if not _trigger:
            _id_fail = _id_chk = 0
            for _r in _page_qlines:
                try:
                    _tp_e = _r.fields.get("total_price_excl_tax")
                    _tx_r = _r.fields.get("tax_rate")
                    _tx_a = _r.fields.get("tax_amount")
                    if _tp_e is not None and _tx_r is not None and _tx_a is not None:
                        _exp = float(_tp_e) * float(_tx_r)
                        if _exp > 0:
                            _id_chk += 1
                            if abs(float(_tx_a) - _exp) / max(_exp, 1) > _TAX_IDENTITY_TOL:
                                _id_fail += 1
                except (TypeError, ValueError):
                    pass
            if _id_chk > 0 and _id_fail / _id_chk > _TAX_IDENTITY_FAIL_RATE:
                _trigger = True
        if _trigger:
            _pages_needing_tax_retry.append(_pno)

    if _pages_needing_tax_retry:
        log.info("tax-field retry triggered for page(s): %s", _pages_needing_tax_retry)

        def _tax_retry_one(_pno: int):
            """Run tax-field retry for a single page. Returns (page_no, retry_rows, retry_metric)."""
            _retry_rows, _retry_metric = _process_page(
                _pno, _pno - 1,
                page_htmls[_pno - 1],
                page_imgs[_pno],
                role_by_page.get(_pno),
                provider,
                adapter,
                page_rotations.get(_pno, 0),
                inherited_header_by_page.get(_pno),
            )
            return _pno, _retry_rows, _retry_metric

        _tax_workers = min(PAGE_CONCURRENCY, len(_pages_needing_tax_retry))
        with ThreadPoolExecutor(max_workers=_tax_workers) as _tax_exc:
            _tax_futs = {
                _tax_exc.submit(_tax_retry_one, _pno): _pno
                for _pno in _pages_needing_tax_retry
            }
            for _fut in as_completed(_tax_futs):
                _pno = _tax_futs[_fut]
                _orig_rows, _orig_metric = results_by_page[_pno]
                _orig_score = _page_tax_quality(_orig_rows)
                try:
                    _, _retry_rows, _retry_metric = _fut.result()
                    _retry_score = _page_tax_quality(_retry_rows)
                    _use_retry = _retry_score > _orig_score
                    log.info(
                        "tax-field retry page %d: orig=%s retry=%s → %s",
                        _pno, _orig_score, _retry_score,
                        "using retry" if _use_retry else "keeping original",
                    )
                    if _use_retry:
                        results_by_page[_pno] = (_retry_rows, _retry_metric)
                except Exception as _exc:
                    log.warning("tax-field retry page %d failed: %s", _pno, _exc)

    # 懒渲染内存释放：逐页 LLM/tiling/tax-retry 全部完成，高清图字节不再需要。
    # 必须在 tax-retry 块之后（retry 仍读 page_imgs[_pno]）。
    page_imgs.clear()

    recall_rows_buf: list[DraftRow] = []
    for page_no in sorted(results_by_page):
        rows, metric = results_by_page[page_no]
        if page_no in recall_set:
            # 召回页行先隔离，过门禁后再合入（见 _filter_recall_rows）
            recall_rows_buf.extend(rows)
            # metric 不进质量门（避免拖累 under_extraction 等置信目标页统计）
        else:
            all_rows.extend(rows)
            page_metrics.append(metric)

    # 召回行过门禁：满足条件的合入 all_rows（成为正式报价行）；不满足的隔离进
    # review_candidates，**不进 rows / 不进比价 / 不入库**，仅供核对 UI 人工裁决。
    review_candidates: list[DraftRow] = []
    if recall_rows_buf:
        accepted_recall, review_candidates = _filter_recall_rows(
            recall_rows_buf, all_rows, adapter.name_key)
        log.info("recall rows: %d accepted → official rows, %d → review_candidates (isolated)",
                 len(accepted_recall), len(review_candidates))
        all_rows.extend(accepted_recall)

    # ── 跨页去重（防止相邻页面重叠或转置表列重复） ─────────────────────────
    all_rows = _dedup_cross_page(all_rows, adapter.name_key)

    # ── 缺失序号推断（仅当前后邻居 seq 缺口恰好为 2，且自身有物料名称时） ────────
    all_rows = _infer_missing_seqs(all_rows)

    # ── 算术一致性门禁（qty×单价≈合价；检测列识别错误，标记进REVIEW，不改原值） ──
    all_rows = _validate_arithmetic(all_rows)

    # ── Meta 提取（可选，各侧自实现） ────────────────────────────────────
    meta: dict = {}
    if adapter.extract_meta:
        _notify("提取文档元信息", 83)
        try:
            # 传所有 OCR 过的页（meta_extra ∪ extract_pages，排除 recall）；
            # 各 adapter 内部用 _is_brand_page / classify_page 做内容过滤，
            # 不依赖视觉分类（品牌页偶被误判为 table continuation 时仍可找到）。
            non_target_htmls = [
                (p, html_by_page[p]) for p in sorted(html_by_page)
                if p not in recall_set and html_by_page.get(p)
            ]
            meta = adapter.extract_meta(non_target_htmls, provider) or {}
        except Exception as exc:
            log.warning("recognize_tables: extract_meta failed: %s", exc)
            meta = {"meta_error": str(exc)}

    # ── 质量报告 ─────────────────────────────────────────────────────────
    declared_total: float | None = meta.get("declared_total")
    quality = compute_quality(
        rows=all_rows,
        page_metrics=page_metrics,
        total_pages=total_pages,
        target_pages=tgt,
        declared_total=declared_total,
        truncated=truncated,
        rendered_pages=rendered_pages,
        ocr_success_pages=ocr_success,
        ocr_failed_pages=ocr_failed,
        ocr_failed_indices=ocr_failed_indices,
        failed_target_pages=failed_target,
    )

    log.info(
        "recognize_tables[%s]: quality=%s rows=%d (quote_lines=%d) pages=%d/%d",
        adapter.doc_type, quality.status,
        len(all_rows), quality.quote_line_count,
        quality.processed_pages, total_pages,
    )

    # ── Excel 对账（可选） ────────────────────────────────────────────────
    reconcile_result: dict | None = None
    if xlsx_path:
        _notify("Excel对账", 92)
        try:
            reconcile_result = _reconcile_vs_excel(
                adapter.doc_type, all_rows, xlsx_path, adapter.name_key
            )
        except Exception as exc:
            log.error("recognize_tables: excel reconcile failed: %s", exc)
            reconcile_result = {"error": str(exc)}

    _notify("完成", 98)
    return ExtractionDraft(
        doc_type=adapter.doc_type,
        source_file=str(file_path),
        page_count=total_pages,
        processed_page_count=len(page_metrics),
        target_pages=tgt,
        rows=all_rows,
        meta=meta,
        quality=quality,
        reconcile=reconcile_result,
        review_candidates=review_candidates,
    )


# ─── 单页处理 ─────────────────────────────────────────────────────────────────

# ─── 页面方向纠正（OCR 质量触发，零依赖，按文档类型拆分信号） ──────────────────
# 部分扫描页被旋转 90° 存储，OCR 对侧向文字识别会列错位。用「表格列语义覆盖度」作为
# 方向质量信号；文档级仅产生候选方向，逐页比较 original vs rotated，唯一严格更高者才替换。
# 不针对任一文件，换页码/换公司同样适用；支持混合方向；90/270 并列不旋转。
_QUOTE_CORE_SLOTS  = {"name", "spec", "unit", "qty",
                      "unit_price", "unit_price_excl_tax", "total_price"}
_QUOTE_PRICE_SLOTS = {"unit_price", "unit_price_excl_tax", "total_price"}
_TENDER_CORE_SLOTS = {"name", "spec", "unit", "qty"}
_ORIENT_MIN_GOOD = 3            # 核心列覆盖 ≥ 此值视为方向正常
_ORIENT_ROTATIONS = (90, 270)   # 仅尝试两个 90° 方向（180° 对表格罕见）
_ORIENT_SAMPLE_K = 4            # 文档级检测抽样页数


def _orientation_quality(html: str, page_no: int, doc_type: str = "quote") -> int:
    """方向质量 = 最大表格的核心列语义覆盖数；无 grids 返回 0。

    信号按文档类型拆分（item 3）：
    - 报价表：name/spec/unit/qty + 价格列；要有数量或任一价格列才算可信表头。
    - 采购清单：name/spec/unit/qty（无价格）；要有名称且规格或数量。
    """
    from apps.api.intelligence.table_parser import html_to_table_grids
    try:
        grids = html_to_table_grids(html, page_no)
    except Exception:
        return 0
    if not grids:
        return 0
    is_tender = (doc_type == "tender")
    core = _TENDER_CORE_SLOTS if is_tender else _QUOTE_CORE_SLOTS
    best = 0
    for g in grids:
        slots = set(g.col_map.values()) & core
        score = len(slots)
        if is_tender:
            if "name" not in slots or not (slots & {"spec", "qty"}):
                score = min(score, 1)
        else:
            if "qty" not in slots and not (slots & _QUOTE_PRICE_SLOTS):
                score = min(score, 1)
        best = max(best, score)
    return best


def _orientation_signal(html: str, doc_type: str) -> bool:
    """该页是否「值得探测旋转」——按文档类型用不同关键词，不只看价格。"""
    if doc_type == "tender":
        return any(k in html for k in ("序号", "名称", "规格", "数量", "单位", "项目"))
    return _html_has_price(html) or any(k in html for k in ("数量", "规格", "单价", "合价"))


def _rotate_png_bytes(image: bytes, degrees: int) -> bytes:
    import io
    from PIL import Image
    with Image.open(io.BytesIO(image)) as im:
        rotated = im.convert("RGB").rotate(-degrees, expand=True)  # PIL 正角=逆时针
        buf = io.BytesIO()
        rotated.save(buf, "PNG")
        return buf.getvalue()


def _contiguous_runs(pages: list[int]) -> list[list[int]]:
    """把页号切成连续段（[4,5,6,9,10] → [[4,5,6],[9,10]]）。"""
    runs: list[list[int]] = []
    for p in sorted(pages):
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    return runs


def _detect_chain_orientation(
    chain: list[int], page_htmls: list[str], page_imgs: dict[int, bytes],
    provider: Any, doc_type: str,
) -> tuple[int, dict[int, tuple[str, bytes]]]:
    """连续表链方向检测：返回 (angle, probe_cache)。

    angle: 0/90/270 — 整条链统一使用的方向角。
    probe_cache: {page_no: (html, rotated_image)} — 探测阶段 sample 页在 winning
        angle 下的 OCR 结果，供调用方直接使用、无需重复 OCR。

    取代旧 `_detect_doc_rotation` 的「>50% 抽样投票」：
      1) 若链内多数页在 0° 已列覆盖达标（≥MIN_GOOD），直接返回 (0, {})，不探测。
      2) 否则对 sample 页在 {90,270} 各 OCR 一次，按列覆盖度求和取 argmax；
         唯一且严格高于 0° 才旋转；并列或不及 0° 则返回 (0, {})。
    """
    if not chain:
        return 0, {}
    q0 = {p: _orientation_quality(page_htmls[p - 1], p, doc_type) for p in chain}
    good0 = sum(1 for q in q0.values() if q >= _ORIENT_MIN_GOOD)
    if good0 >= max(1, len(chain) // 2):
        return 0, {}   # 已正立：不探测

    anchors = [p for p in chain
               if _orientation_signal(page_htmls[p - 1], doc_type)] or list(chain)
    step = max(1, len(anchors) // _ORIENT_SAMPLE_K)
    sample = anchors[::step][:_ORIENT_SAMPLE_K]

    scores: dict[int, int] = {0: sum(q0[p] for p in sample)}
    # probe_by_deg[deg][page] = (html, rotated_image)
    probe_by_deg: dict[int, dict[int, tuple[str, bytes]]] = {deg: {} for deg in _ORIENT_ROTATIONS}
    for deg in _ORIENT_ROTATIONS:
        s = 0
        for p in sample:
            try:
                rb = _rotate_png_bytes(page_imgs[p], deg)
                results, _f = provider.ocr_pages_with_roles([rb])
                if results:
                    html = results[0][1]
                    s += _orientation_quality(html, p, doc_type)
                    probe_by_deg[deg][p] = (html, rb)
            except Exception as exc:
                log.warning("chain orient probe failed page %d deg %d: %s", p, deg, exc)
        scores[deg] = s

    best_deg = max(_ORIENT_ROTATIONS, key=lambda d: scores[d])
    winners = [d for d in _ORIENT_ROTATIONS if scores[d] == scores[best_deg]]
    chosen = best_deg if (scores[best_deg] > scores[0] and len(winners) == 1) else 0
    log.info("chain %s-%s orient scores=%s sample=%s -> %d°",
             chain[0], chain[-1], scores, sample, chosen)
    probe_cache = probe_by_deg.get(chosen, {}) if chosen else {}
    return chosen, probe_cache


def _correct_page_orientation(
    html: str, image: bytes, page_no: int, provider: Any,
    doc_type: str, candidates: set[int],
) -> tuple[str, bytes, int]:
    """[生产路径已停用] 逐页方向纠正：比较 original 与各候选方向的 quality，唯一严格更高者才替换。

    生产链路（recognize_tables）已改为 _detect_chain_orientation + 链方向直接应用，
    不再调用此函数。此函数仅保留供 test_orientation_correction.py 单元测试引用。
    如需修改旋转逻辑，修改 _detect_chain_orientation 和 recognize_tables 中的链应用块。

    支持混合方向（每页独立判定）与单页旋转；最高质量在多个方向并列（含与原图并列）
    时不旋转（item 2：90/270 并列不得自动旋转）。返回 (html, image, rotation_deg)。
    """
    if not candidates:
        return html, image, 0
    q0 = _orientation_quality(html, page_no, doc_type)
    scored: list[tuple[int, int, str, bytes]] = [(0, q0, html, image)]
    for deg in sorted(candidates):
        try:
            rb = _rotate_png_bytes(image, deg)
            results, _f = provider.ocr_pages_with_roles([rb])
            if results:
                rq = _orientation_quality(results[0][1], page_no, doc_type)
                scored.append((deg, rq, results[0][1], rb))
        except Exception as exc:
            log.warning("orientation apply failed page %d deg %d: %s", page_no, deg, exc)
    max_q = max(s[1] for s in scored)

    # 情形 A：可测量且旋转严格更高 → 唯一胜出者替换；并列不转
    if max_q > q0:
        winners = [s for s in scored if s[1] == max_q]
        if len(winners) == 1 and winners[0][0] != 0:
            deg, q, h, img = winners[0]
            log.info("Page %d orientation corrected %d° (q %d→%d)", page_no, deg, q0, q)
            return h, img, deg
        return html, image, 0   # 多方向并列更高 → 不自动旋转

    # 情形 B：无表头续表页测不出 quality（q0=0 且旋转后仍 0）。
    # 仅当文档恰有【唯一】候选方向时，信任文档级信号对其转正；混合方向(>1候选)不处理。
    if q0 == 0 and len(candidates) == 1:
        deg = next(iter(candidates))
        for d, q, h, img in scored:
            if d == deg:
                log.info("Page %d orientation applied %d° (headerless, doc single-candidate)",
                         page_no, deg)
                return h, img, deg
    return html, image, 0


def _process_page(
    page_no: int,
    page_idx: int,
    html: str,
    image: bytes,
    page_cls: Any,
    provider: Any,
    adapter: RecognizeAdapter,
    rotation_applied: int = 0,
    inherited_header: list[str] | None = None,
) -> tuple[list[DraftRow], PageMetric]:
    """单页完整处理：build_input → LLM → retry → tiling → DraftRow[]。

    方向纠正在 recognize_tables 文档级完成；rotation_applied 仅用于审计记录。
    inherited_header: 前一页的列头列表，供无表头续表页继承（见 html_to_table_grids）。
    """
    from apps.api.intelligence.table_parser import html_to_table_grids

    # page_cls 可能是 VisualPageClassification(.role) 或旧 PageClassification(.primary_role)
    _r = getattr(page_cls, "role", None) or getattr(page_cls, "primary_role", None)
    role = _r.value if hasattr(_r, "value") else str(_r)

    if not html.strip():
        metric = PageMetric(
            page=page_no, page_index=page_idx, role=role,
            table_count=0, row_count=0,
            input_mode="html_fallback", fallback_reason="empty_html",
            rotation_applied=rotation_applied,
        )
        return [], metric

    # ── 计数 table_count / row_count ──────────────────────────────────────
    table_count = html.count("<table")
    row_count = html.count("<tr")

    def _prompt(mode: str) -> str:
        if adapter.prompt_for_mode:
            return adapter.prompt_for_mode(mode)
        return adapter.row_prompt

    grids = None

    # ── LLM path ──────────────────────────────────────────────────────────
    llm_input, expected_rows, input_mode, fallback_reason = _build_llm_input(
        html, page_no, inherited_header=inherited_header)

    # ── First attempt ─────────────────────────────────────────────────
    data, raw_rows = _llm_extract(provider, _prompt(input_mode), llm_input)
    thinking_retry = False

    # ── expected_rows gate → thinking retry ───────────────────────────
    if expected_rows > 0 and len(raw_rows) < expected_rows * _EXPECTED_ROWS_MIN_RATIO:
        log.warning(
            "Page %d: extracted=%d expected~=%d → thinking retry",
            page_no, len(raw_rows), expected_rows,
        )
        data2, raw_rows2 = _llm_extract(provider, _prompt(input_mode), llm_input, thinking=True)
        if len(raw_rows2) > len(raw_rows):
            raw_rows = raw_rows2
        thinking_retry = True

    # ── Adaptive tiling fallback ───────────────────────────────────────
    tiled = False
    tile_count = 0
    should_tile = (
        # Case 1: expected rows known but under-extracted (including after thinking retry)
        (expected_rows > 0 and len(raw_rows) < expected_rows * _EXPECTED_ROWS_MIN_RATIO)
        # Case 2: table structure present but parser failed (transposed / complex layout)
        or (fallback_reason == "no_grids" and len(raw_rows) == 0 and table_count > 0)
        # Case 3: html_fallback + zero rows + price keyword signal
        or (input_mode == "html_fallback" and len(raw_rows) == 0
            and _html_has_price(html))
    )
    if should_tile:
        log.info("Page %d: triggering adaptive tiling", page_no)
        tiled_rows, tile_count = _try_tiled_extraction(
            page_no, image, provider, adapter
        )
        if len(tiled_rows) > len(raw_rows):
            raw_rows = tiled_rows
            tiled = True
            input_mode = "tiled"
            fallback_reason = ""

    # reuse pre-parsed grids; if tiled, full-page grids are irrelevant
    if tiled:
        grids = None
    elif grids is None:
        try:
            grids = html_to_table_grids(html, page_no, inherited_header=inherited_header)
        except Exception:
            grids = None

    draft_rows = _raw_items_to_draft_rows(raw_rows, page_no, grids, adapter.name_key)
    for r in draft_rows:
        r.fields.setdefault("parser_mode", "llm")

    metric = PageMetric(
        page=page_no,
        page_index=page_idx,
        role=role,
        table_count=table_count,
        row_count=row_count,
        input_mode=input_mode,
        fallback_reason=fallback_reason,
        expected_rows=expected_rows,
        extracted_rows=len([r for r in draft_rows if r.row_type == "quote_line"]),
        thinking_retry=thinking_retry,
        tiled=tiled,
        tile_count=tile_count,
        rotation_applied=rotation_applied,
    )
    return draft_rows, metric


def _llm_extract(
    provider: Any,
    prompt: str,
    llm_input: str,
    thinking: bool = False,
) -> tuple[dict, list[dict]]:
    data, _raw, _tok = provider._llm_call_json(prompt, llm_input, enable_thinking=thinking)
    items = data.get("items") or []
    return data, items


# ─── Adaptive tiling ──────────────────────────────────────────────────────────

def _try_tiled_extraction(
    page_no: int,
    image: bytes,
    provider: Any,
    adapter: RecognizeAdapter,
) -> tuple[list[dict], int]:
    """切片 → 并发 OCR+LLM → 合并去重。返回 (raw_items, n_tiles)。

    4 个切片并发执行，每个切片需 1 次 OCR + 1 次 LLM 调用。
    并发受 OCR provider 的 per-key Semaphore 保护，不会触发 429。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from apps.api.intelligence.adaptive_tiler import tile_page, dedup_raw_items

    tiles = tile_page(image)
    all_items: list[dict] = []

    def _process_tile(tile):
        tile_html = _ocr_tile(provider, tile.image_bytes)
        llm_input, _exp, _mode, _reason = _build_llm_input(tile_html, page_no)
        tile_data, tile_items = _llm_extract(provider, adapter.row_prompt, llm_input)
        for item in tile_items:
            item["_tile_bbox"] = list(tile.bbox_pct)
        return tile_items

    _tile_workers = min(PAGE_CONCURRENCY, len(tiles))
    with ThreadPoolExecutor(max_workers=_tile_workers) as _tile_exc:
        _tile_futs = {_tile_exc.submit(_process_tile, t): t for t in tiles}
        for _fut in as_completed(_tile_futs):
            tile = _tile_futs[_fut]
            try:
                all_items.extend(_fut.result())
            except Exception as exc:
                log.warning("tile %d on page %d failed: %s",
                            tile.tile_index, page_no, exc)

    deduped = dedup_raw_items(all_items, name_key=adapter.name_key)
    log.info(
        "tiled page %d: %d tiles → %d items before dedup → %d after",
        page_no, len(tiles), len(all_items), len(deduped),
    )
    return deduped, len(tiles)


def _ocr_tile(provider: Any, image_bytes: bytes) -> str:
    """对单条切片做 Stage-1 OCR，返回 HTML 字符串。

    使用 ocr_pages_with_roles([tile_image]) 取第一个结果的 html。
    这是公共接口，不依赖 _ocr_page 私有方法。
    """
    results, _failures = provider.ocr_pages_with_roles([image_bytes])
    if results:
        _cls, html = results[0]
        return html
    return ""


# ─── LLM input builder（从 tender_pdf.py 共享） ───────────────────────────────

def _build_llm_input(html: str, page_no: int,
                     inherited_header: list[str] | None = None) -> tuple[str, int, str, str]:
    """准备 Stage-2 LLM 输入，同时估算期望行数。

    Returns:
        (llm_input_str, expected_rows, input_mode, fallback_reason)

    input_mode: "table_grid" | "html_fallback"

    ``inherited_header``: column header list from the preceding page; passed to
    html_to_table_grids so continuation pages without their own header row can
    still produce a structured grid (avoids costly html_fallback LLM re-transcription).
    """
    from apps.api.intelligence.table_parser import html_to_table_grids

    grids = html_to_table_grids(html, page_num=page_no, inherited_header=inherited_header)
    if grids:
        all_unique_headers = all(len(set(g.header)) == len(g.header) for g in grids)
        expected_rows = sum(g.quote_line_count() for g in grids)

        if all_unique_headers:
            rows_out = []
            for g in grids:
                rows = [
                    {"row_index": r.row_index, "row_type": r.row_type, "cells": r.cells}
                    for r in g.rows
                    if r.row_type not in (RT_INVALID, RT_GRAND_TOTAL, RT_SUBTOTAL)
                ]
                if rows:
                    rows_out.append({"page": g.page, "table_index": g.table_index, "rows": rows})
            return (
                json.dumps(rows_out, ensure_ascii=False, separators=(",", ":")),
                expected_rows,
                "table_grid",
                "",
            )
        else:
            return _stripped_html(html), expected_rows, "html_fallback", "duplicate_headers"

    return _stripped_html(html), 0, "html_fallback", "no_grids"


_STYLE_RE = __import__("re").compile(r"<style[^>]*>.*?</style>", __import__("re").DOTALL | __import__("re").IGNORECASE)
_SCRIPT_RE = __import__("re").compile(r"<script[^>]*>.*?</script>", __import__("re").DOTALL | __import__("re").IGNORECASE)
_OUTER_RE = __import__("re").compile(r"</?(?:html|head|body)[^>]*>", __import__("re").IGNORECASE)
_WS_RE = __import__("re").compile(r"[ \t]{2,}")


def _stripped_html(html: str) -> str:
    html = _STYLE_RE.sub("", html)
    html = _SCRIPT_RE.sub("", html)
    html = _OUTER_RE.sub("", html)
    html = _WS_RE.sub(" ", html)
    return html.strip()


def _html_has_price(html: str) -> bool:
    return any(s in html for s in ["单价", "合价", "价税合计", "含税"])


# ─── DraftRow 构建 ─────────────────────────────────────────────────────────────

def _raw_items_to_draft_rows(
    items: list[dict],
    page_no: int,
    grids: list | None,
    name_key: str,
) -> list[DraftRow]:
    """把 LLM raw items 转为 DraftRow，并分配 source_ref。

    grids: 从当前页 HTML 解析的 TableGrid 列表（无 tiling 时用）；None 则 bbox=None。
    """
    # 建立 (table_index, row_index) → 行类型映射（用于校验 LLM 返回的索引）
    valid_quote_pairs: set[tuple[int, int]] = set()
    if grids:
        for g in grids:
            for r in g.rows:
                if r.row_type == "quote_line":
                    valid_quote_pairs.add((g.table_index, r.row_index))

    draft_rows: list[DraftRow] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        # ── row_type ─────────────────────────────────────────────────────
        row_type = str(item.get("row_type") or "quote_line").lower()
        # LLM 可能返回 grand_total / subtotal — 禁止污染 quote_lines
        if row_type not in ("quote_line", "subtotal", "grand_total",
                             "section_header", "remark", "invalid"):
            row_type = "quote_line"

        # ── source_ref ────────────────────────────────────────────────────
        tile_bbox = item.pop("_tile_bbox", None)
        t_idx = item.pop("table_index", None)
        r_idx = item.pop("row_index", i)
        try:
            t = int(t_idx) if t_idx is not None else 0
            r = int(r_idx)
        except (TypeError, ValueError):
            t, r = 0, i

        source_ref = SourceRef(page=page_no, table=t, row=r)
        if tile_bbox:
            source_ref.tile_bbox = tuple(tile_bbox)

        # ── raw_cells: 原始单元格快照 ───────────────────────────────────
        raw_cells = {k: v for k, v in item.items()}

        # ── fields: 标准化字段（完整 §4 超集） ───────────────────────────
        fields = _normalize_fields(item, name_key)

        # ── 空价格行降级：quote_line 但所有数量/价格字段均为空 → invalid ─────
        # 防止 thinking retry 把页脚合计行或表头行误抽取为报价行。
        # 合法报价行至少要有 qty 或任一价格字段；全为 None 说明是非商品行被误分类。
        _PRICE_KEYS = (
            "qty", "unit_price", "unit_price_incl_tax", "unit_price_excl_tax",
            "total_price", "total_price_incl_tax", "total_price_excl_tax",
        )
        extra_flags: list[str] = []
        if row_type == "quote_line" and all(fields.get(k) is None for k in _PRICE_KEYS):
            row_type = "invalid"
            extra_flags = ["no_numeric_fields"]

        # ── 含税价格合理性检查：含税 < 不含税 物理不可能（税率≥0） ──────────────
        # 若含税单价或含税合价 < 对应不含税值，说明 LLM 把税额（行级税额 =
        # total_excl × tax_rate）误映射到了含税字段，仅标记 flag 供人工核对，
        # 不清空字段（保留原始值以便在审核 UI 可见），
        # 单价含税字段清空（因为不参与 total sum，清空更稳定；合价含税字段保留原始值）。
        try:
            _up_incl = fields.get("unit_price_incl_tax")
            _up_excl = fields.get("unit_price_excl_tax")
            _tp_excl = fields.get("total_price_excl_tax")
            _tax_rt  = fields.get("tax_rate")
            _suspicious = False
            if _up_incl is not None and float(_up_incl) > 0:
                # 检查1：含税单价 < 不含税单价（税率≥0时物理不可能）
                if (_up_excl is not None and float(_up_excl) > 0
                        and float(_up_incl) < float(_up_excl) * 0.999):
                    _suspicious = True
                # 检查2：含税单价 ≈ 不含税合价 × 税率（行级税额误填为单价）
                # 正确值: unit_incl = unit_excl × (1+tax_rate)；错误值 = total_excl × tax_rate
                if (not _suspicious and _tp_excl is not None and _tax_rt is not None
                        and float(_tp_excl) > 0 and float(_tax_rt) > 0):
                    _expected_row_tax = float(_tp_excl) * float(_tax_rt)
                    if abs(float(_up_incl) - _expected_row_tax) / max(float(_up_incl), 1) < 0.01:
                        _suspicious = True
            if _suspicious:
                fields["unit_price_incl_tax"] = None  # 不参与 sum，清空避免算术误报
                extra_flags = list(extra_flags) + ["incl_tax_unit_price_suspicious"]
        except (TypeError, ValueError):
            pass
        try:
            _tp_incl = fields.get("total_price_incl_tax")
            _tp_excl = fields.get("total_price_excl_tax")
            if (
                _tp_incl is not None and _tp_excl is not None
                and float(_tp_incl) < float(_tp_excl) * 0.999
                and float(_tp_excl) > 0
            ):
                # 合价含税字段保留原始值（参与 sum 比较），仅 flag 供人工核查
                extra_flags = list(extra_flags) + ["total_incl_lt_excl_suspicious"]
        except (TypeError, ValueError):
            pass

        draft_rows.append(DraftRow(
            row_index=i,
            row_type=row_type,
            raw_cells=raw_cells,
            fields=fields,
            source_ref=source_ref,
            validation_flags=extra_flags,
        ))

    return draft_rows


def _normalize_fields(item: dict, name_key: str) -> dict:
    """统一字段名到标准超集（见 CLAUDE.md §4）。"""
    def _s(k: str) -> str:
        return str(item.get(k) or "").strip()

    def _f(k: str):
        v = item.get(k)
        if v is None or v == "":
            return None
        try:
            return float(str(v).replace(",", "").replace("，", ""))
        except (ValueError, TypeError):
            return None

    # 名称字段：招标用 name，报价用 material
    name = _s(name_key) or _s("name") or _s("material")

    return {
        "seq": _s("seq"),
        "name": name,
        "raw_name": _s("raw_name") or name,
        "spec": _s("spec"),
        "raw_spec": _s("raw_spec") or _s("spec"),
        "model": _s("model"),
        "pressure": _s("pressure"),
        "unit": _s("unit"),
        "qty": _f("qty"),
        "unit_price": _f("unit_price"),
        "unit_price_incl_tax": _f("unit_price_incl_tax"),
        "unit_price_excl_tax": _f("unit_price_excl_tax"),
        "tax_rate": _f("tax_rate"),
        "tax_amount": _f("tax_amount"),
        "total_price": _f("total_price"),
        "total_price_incl_tax": _f("total_price_incl_tax"),
        "total_price_excl_tax": _f("total_price_excl_tax"),
        "brand": _s("brand"),
        "profession": _s("profession"),
        "remark": _s("remark"),
        "materials": item.get("materials") or {},
        "material_type": _s("material_type"),
        "canonical": item.get("canonical") or {},
        "normalized_material": _s("normalized_material"),
        "ocr_correction_reason": _s("ocr_correction_reason"),
    }


# ─── Excel 对账（可选路径） ────────────────────────────────────────────────────

def _reconcile_vs_excel(
    doc_type: str,
    rows: list[DraftRow],
    xlsx_path: str,
    name_key: str,
) -> dict:
    """对比 ExtractionDraft rows 与 Excel ground truth。"""
    if doc_type == "tender":
        from apps.api.services.tender_list import parse_tender_xlsx
        from apps.api.services.source_reconcile import reconcile_anchors
        xlsx_anchors = parse_tender_xlsx(xlsx_path)
        xlsx_items = [
            {"seq": str(a.seq), "name": a.name, "spec": a.spec,
             "unit": a.unit, "qty": a.qty}
            for a in xlsx_anchors
        ]
        pdf_items = [
            {"seq": r.fields.get("seq") or "",
             "name": r.fields.get("name") or "",
             "spec": r.fields.get("spec") or "",
             "unit": r.fields.get("unit") or "",
             "qty": r.fields.get("qty")}
            for r in rows if r.row_type == "quote_line"
        ]
        return reconcile_anchors(xlsx_items, pdf_items, source_type="pdf_primary")
    # 报价侧对账：简单行数 + 声明总价检查
    return _reconcile_quote_vs_excel(rows, xlsx_path, name_key)


def _reconcile_quote_vs_excel(
    rows: list[DraftRow],
    xlsx_path: str,
    name_key: str,
) -> dict:
    """报价侧简单对账：Excel 行数、声明总价 vs 明细合计。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        xlsx_row_count = sum(1 for row in ws.iter_rows(min_row=2) if any(c.value for c in row))
    except Exception as exc:
        return {"error": f"excel parse failed: {exc}"}

    pdf_quote_lines = [r for r in rows if r.row_type == "quote_line"]
    return {
        "xlsx_row_count": xlsx_row_count,
        "pdf_row_count": len(pdf_quote_lines),
        "row_count_match": xlsx_row_count == len(pdf_quote_lines),
    }


# ─── helper ───────────────────────────────────────────────────────────────────

def _dedup_cross_page(rows: list[DraftRow], name_key: str) -> list[DraftRow]:
    """跨页去重：防止相邻页面重叠或转置表同列被多切片抽到同一物料。

    策略：
    1. 有整数 seq → seq 是全局唯一标识；多份候选保留字段最完整的版本
       （避免 tiling 产生的 off-column 版本覆盖正确版本）。
    2. seq 为空 → **默认不自动删除**。当前 source_ref.row 在 LLM 不返回
       row_index 时退化为数组序号、table 默认 0，并非稳定 OCR 物理坐标，
       既可能误删合法行也可能漏删真重叠，不能作为通用自动去重依据。
       因此无序号行一律保留；当 (page, table, row) 来源身份与已见行碰撞且
       业务指纹一致时，仅标记 validation_flags=["possible_duplicate"]，
       交质量报告/用户确认，绝不静默删除。
       待确定性 TableGrid→DraftRow 路径建立真实行坐标后再开启自动去重。
    3. name/spec 均空 → 保留（不合并）。
    """
    # ── Pass 1: collect all quote_lines grouped by seq key ────────────────
    seq_candidates: dict[str, list[DraftRow]] = {}
    no_seq_rows: list[DraftRow] = []
    non_quote: list[DraftRow] = []

    for row in rows:
        if row.row_type != "quote_line":
            non_quote.append(row)
            continue

        f = row.fields
        seq = str(f.get("seq") or "").strip()

        if seq.isdigit():
            seq_candidates.setdefault(seq, []).append(row)
        else:
            name = str(f.get(name_key) or f.get("name") or "").strip()
            spec = str(f.get("spec") or "").strip()
            if name or spec:
                no_seq_rows.append(row)
            else:
                non_quote.append(row)  # truly unnamed → keep as-is

    # ── Pass 2: for integer-seq groups, pick the best candidate ───────────
    quote_lines: list[DraftRow] = []
    for seq, candidates in seq_candidates.items():
        if len(candidates) == 1:
            quote_lines.append(candidates[0])
        else:
            best = max(candidates, key=_row_quality_score)
            log.debug("cross-page dedup: seq=%s → kept 1 of %d (best quality)", seq, len(candidates))
            quote_lines.append(best)

    # ── Pass 2b: no-seq rows — keep ALL, only flag possible duplicates ────
    # 不删除任何无序号行。只有当 (page,table,row) 来源身份 *且* 业务指纹
    # (name,spec,qty,total) 都与已见行完全一致时，标记 possible_duplicate，
    # 由下游质量门/用户判定，避免静默删掉合法报价（如 23 万元的不同系统采购行）。
    seen: dict[tuple, DraftRow] = {}
    flagged = 0
    for row in no_seq_rows:
        src = row.source_ref
        f = row.fields
        name = str(f.get(name_key) or f.get("name") or "").strip()
        spec = str(f.get("spec") or "").strip()
        qty = str(f.get("qty") or "").strip()
        total = str(f.get("total_price") or f.get("total_price_incl_tax") or "").strip()
        src_key = (src.page, src.table, src.row) if src is not None else (0, 0, 0)
        fp_key = (src_key, name, spec, qty, total)
        if fp_key in seen and "possible_duplicate" not in row.validation_flags:
            row.validation_flags.append("possible_duplicate")
            flagged += 1
        else:
            seen[fp_key] = row
        quote_lines.append(row)
    if flagged:
        log.info("cross-page dedup: %d no-seq row(s) flagged possible_duplicate (kept, not removed)", flagged)

    # ── Restore page order ─────────────────────────────────────────────────
    page_order: dict[int, int] = {}
    for i, row in enumerate(rows):
        p = row.source_ref.page if row.source_ref else 0
        page_order[id(row)] = (p, i)

    quote_lines.sort(key=lambda r: page_order.get(id(r), (999, 999)))

    result = non_quote + quote_lines
    result.sort(key=lambda r: page_order.get(id(r), (999, 999)))

    removed = len(rows) - len(result)
    if removed:
        log.info("cross-page dedup: %d → %d (removed %d duplicates)", len(rows), len(result), removed)
    return result


def _infer_missing_seqs(rows: list[DraftRow]) -> list[DraftRow]:
    """当前后邻居 seq 缺口恰好为 2 时，为中间的无序号行推断 seq。

    条件（全部满足才推断）：
    - 行本身是 quote_line 且无 seq
    - 前后最近的有整数 seq 邻居的差值恰好为 2
    - 行有物料名称（不是空行）

    只在"绝大多数行都有 seq"的文档中触发，避免在无序号文档中误填。
    """
    quote_lines = [r for r in rows if r.row_type == "quote_line"]
    if not quote_lines:
        return rows

    seq_count = sum(1 for r in quote_lines if str(r.fields.get("seq") or "").strip().isdigit())
    if seq_count < len(quote_lines) * 0.5:
        return rows  # not a seq-based document

    inferred = 0
    for i, row in enumerate(rows):
        if row.row_type != "quote_line":
            continue
        if str(row.fields.get("seq") or "").strip().isdigit():
            continue
        if not str(row.fields.get("name") or row.fields.get("material") or "").strip():
            continue

        prev_seq = next_seq = None
        for j in range(i - 1, -1, -1):
            s = str(rows[j].fields.get("seq") or "").strip()
            if s.isdigit():
                prev_seq = int(s)
                break
        for j in range(i + 1, len(rows)):
            s = str(rows[j].fields.get("seq") or "").strip()
            if s.isdigit():
                next_seq = int(s)
                break

        if prev_seq is not None and next_seq is not None and next_seq - prev_seq == 2:
            inferred_seq = prev_seq + 1
            row.fields["seq"] = str(inferred_seq)
            row.fields["seq_source"] = "inferred"
            if "seq_inferred" not in row.validation_flags:
                row.validation_flags.append("seq_inferred")
            log.info("Inferred seq=%d for row (prev=%d next=%d): %s %s",
                     inferred_seq, prev_seq, next_seq,
                     str(row.fields.get("name") or "")[:20],
                     str(row.fields.get("spec") or ""))
            inferred += 1

    if inferred:
        log.info("_infer_missing_seqs: inferred %d seq(s)", inferred)
    return rows


def _validate_arithmetic(rows: list[DraftRow]) -> list[DraftRow]:
    """算术一致性门禁：qty × unit_price ≈ total 不成立 → 标 qty_arithmetic_mismatch。

    定位（CLAUDE.md §6）：这是「检测层」，不是「修复层」。它把列识别错误（如凯硕
    seq=89 OCR 把 qty 读成 1，而 total/unit 自洽暗示 qty=4）暴露出来并进 REVIEW，
    **绝不自动改原值**（不静默把 1 改成 4）。

    口径对齐：优先用含税基(qty×unit_incl≈total_incl)，否则不含税基，否则通用
    unit_price/total_price。三者(qty、单价、合价)齐全且 >0 才校验。

    容差：max(0.05 元, total × 0.5%) —— 兼容单价四舍五入带来的 qty×unit 微小尾差。

    证据：在 fields 写 arith_basis / arith_expected_total / arith_actual_total /
    arith_delta，并给出 arith_suggested_qty(= total ÷ unit_price，仅供 REVIEW 参考，
    不写回 qty)。
    """
    def _n(v):
        if v in (None, ""):
            return None
        try:
            return float(str(v).replace(",", "").replace("，", "").strip())
        except (TypeError, ValueError):
            return None

    flagged = 0
    for row in rows:
        if row.row_type != "quote_line":
            continue
        f = row.fields
        qty = _n(f.get("qty"))
        if qty is None or qty <= 0:
            continue
        # 选一个口径自洽的 (unit, total) 对
        pairs = [
            ("incl", _n(f.get("unit_price_incl_tax")), _n(f.get("total_price_incl_tax"))),
            ("excl", _n(f.get("unit_price_excl_tax")), _n(f.get("total_price_excl_tax"))),
            ("generic", _n(f.get("unit_price")), _n(f.get("total_price"))),
        ]
        basis = unit = total = None
        for b, u, t in pairs:
            if u is not None and u > 0 and t is not None and t > 0:
                basis, unit, total = b, u, t
                break
        if basis is None:
            continue

        expected = qty * unit
        delta = abs(expected - total)
        tol = max(0.05, total * 0.005)
        if delta > tol:
            if "qty_arithmetic_mismatch" not in row.validation_flags:
                row.validation_flags.append("qty_arithmetic_mismatch")
            f["arith_basis"] = basis
            f["arith_expected_total"] = round(expected, 2)
            f["arith_actual_total"] = round(total, 2)
            f["arith_delta"] = round(delta, 2)
            f["arith_suggested_qty"] = round(total / unit, 4)  # 仅参考，不写回 qty
            flagged += 1
            log.info("arithmetic mismatch (%s): qty=%s × unit=%.2f = %.2f ≠ total=%.2f "
                     "(delta=%.2f, suggest qty≈%.2f) name=%s spec=%s",
                     basis, f.get("qty"), unit, expected, total, delta,
                     total / unit, str(f.get("name") or "")[:16], f.get("spec") or "")
    if flagged:
        log.info("_validate_arithmetic: flagged %d row(s) qty_arithmetic_mismatch", flagged)
    return rows


def _row_quality_score(row: DraftRow) -> float:
    """为 dedup 评分：字段填充率 + 算术一致性奖励。"""
    f = row.fields
    filled = sum(
        1 for k, v in f.items()
        if v not in (None, "", {}, [])
        and k not in ("raw_name", "raw_spec", "canonical", "materials",
                      "normalized_material", "ocr_correction_reason")
    )
    # Arithmetic bonus: qty * unit_price ≈ total_price
    try:
        qty = float(f.get("qty") or 0)
        up = float(f.get("unit_price") or f.get("unit_price_incl_tax") or 0)
        tp = float(f.get("total_price") or f.get("total_price_incl_tax") or 0)
        if qty > 0 and up > 0 and tp > 0:
            ratio = abs(qty * up - tp) / max(tp, 1)
            if ratio < 0.05:
                filled += 5
    except (TypeError, ValueError):
        pass
    return float(filled)


def _empty_metric(page_no: int, page_idx: int, reason: str) -> PageMetric:
    return PageMetric(
        page=page_no, page_index=page_idx, role="unknown",
        input_mode="html_fallback", fallback_reason=f"exception:{reason}",
    )
