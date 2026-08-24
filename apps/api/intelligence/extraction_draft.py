"""extraction_draft.py — 表格识别输出契约（识别草稿，未确认）。

识别链路只产出 ExtractionDraft；映射到领域对象（TenderAnchor / BidQuoteLine）
在用户核对确认后由各侧 adapter 完成，不在识别内进行。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from apps.api.core.domain_config import (
    MATCH_ARITHMETIC_PASS_THRESHOLD as _ARITHMETIC_PASS_THRESHOLD,
    EXTRACTION_ARITHMETIC_TOLERANCE,
)


# ─── Source evidence ─────────────────────────────────────────────────────────

@dataclass
class SourceRef:
    page: int
    table: int = 0
    row: int = 0
    # 页码区间终点。识别侧只能确定这一行落在 page..page_end 之间（Paddle 跨页合并表
    # 把续页的行全塞进 begin 页，且不给几何坐标，拆不回物理页——见
    # `paddle_vl._merged_page_spans`）。None 或等于 page 时页码是确定的；大于 page 时
    # 下游必须如实显示"第 page-page_end 页"，**不得把 page 当成确定事实**。
    page_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None   # x0,y0,x1,y1 in page pixels
    tile_bbox: tuple[float, float, float, float] | None = None  # fraction of page if tiled

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"page": self.page, "table": self.table, "row": self.row}
        if self.page_end is not None and self.page_end > self.page:
            d["page_end"] = self.page_end
        if self.bbox is not None:
            d["bbox"] = list(self.bbox)
        if self.tile_bbox is not None:
            d["tile_bbox"] = list(self.tile_bbox)
        return d


# ─── Single extracted row ─────────────────────────────────────────────────────

# 评审 N2：build_draft/parse_csv 是报价与招标共用的解析器（vl_quote.py docstring
# "两种文档的行为差异只在'有哪些列、每列叫什么'"），"这一行是明细而非小计/合计"
# 这个判据同样与文档类型无关——但字面值一直叫 "quote_line"，招标清单里没有一行
# 是"报价"，词表名字跟着共享代码泄了过去。改字面值要牵动 quote_confirmation_
# service.py 的持久化路径与审计日志（audit.normalize_row_type 另有一份 quote
# 专属词表，历史值不宜改），故不改值本身，只给"文档类型无关的明细行"这个概念
# 一个不带 quote 语义的名字，供 tender 侧消费点引用。
DETAIL_ROW_TYPE = "quote_line"


@dataclass
class DraftRow:
    """One row as the recognizer sees it — raw cells preserved, fields standardised."""
    row_index: int
    row_type: str           # quote_line(=DETAIL_ROW_TYPE)|subtotal|grand_total|section_header|remark|invalid
    raw_cells: dict         # original OCR header→value mapping (never modified)
    fields: dict            # standardised fields (§4 superset; missing keys = None/"")
    source_ref: SourceRef
    corrections: list = field(default_factory=list)      # [{field,raw,fixed,reason}]
    validation_flags: list = field(default_factory=list) # [arithmetic_mismatch, ...]
    field_sources: dict = field(default_factory=dict)    # field → direct_cell|missing|derived|llm
    extra_fields: dict = field(default_factory=dict)     # unmapped columns: header_text → raw_value
    # parser_mode lives in fields["parser_mode"] ("llm" | "table_grid_deterministic")


# ─── Per-page diagnostics ─────────────────────────────────────────────────────

@dataclass
class PageMetric:
    page: int
    page_index: int                 # 0-based index in full document
    role: str                       # PageRole value
    table_count: int = 0            # <table> elements on this page
    row_count: int = 0              # total <tr> elements on this page
    input_mode: str = "html_fallback"   # table_grid | html_fallback | tiled
    fallback_reason: str = ""
    expected_rows: int = 0
    extracted_rows: int = 0
    thinking_retry: bool = False
    tiled: bool = False
    tile_count: int = 0
    rotation_applied: int = 0        # 0|90|180|270 — degrees rotated before re-OCR (orientation correction)
    shadow_diff: dict | None = None  # Phase B shadow mode: per-page deterministic-vs-LLM comparison


# ─── Row conservation ledger (doc/19 §L3) ────────────────────────────────────
# 让"丢行"从静默变成必须解释的事。本轮 E2E 里远东 19 页丢了 14 页、招标清单 184 行
# 进 92 行出，全程没有任何一个环节报错——因为没有任何东西知道"应该有多少行"。

@dataclass
class PageDrop:
    """一页产出低于预期时的记账条目。reason 必须能指向具体环节，不接受空值。"""
    page: int
    role: str
    reason: str                 # empty_html | no_table_structure | no_grids | exception:* | under_extracted
    expected: int = 0
    extracted: int = 0
    rotation_applied: int = 0

    @property
    def lost(self) -> int:
        return max(0, self.expected - self.extracted)


@dataclass
class RowLedger:
    """识别阶段的行数守恒台账：应有 → 识别，逐页记录去向。"""
    target_pages: int = 0
    expected_rows: int = 0          # 各目标页 expected_rows 之和（OCR 见到的 <tr> 规模）
    recognized_rows: int = 0        # 实际进入 draft.rows 的行数
    empty_pages: list = field(default_factory=list)   # list[PageDrop] — 颗粒无收的页
    short_pages: list = field(default_factory=list)   # list[PageDrop] — 有产出但低于预期的页

    @property
    def dropped_rows(self) -> int:
        return max(0, self.expected_rows - self.recognized_rows)

    def to_dict(self) -> dict:
        return {
            "target_pages": self.target_pages,
            "expected_rows": self.expected_rows,
            "recognized_rows": self.recognized_rows,
            "dropped_rows": self.dropped_rows,
            "empty_pages": [
                {"page": d.page, "role": d.role, "reason": d.reason,
                 "expected": d.expected, "rotation_applied": d.rotation_applied}
                for d in self.empty_pages
            ],
            "short_pages": [
                {"page": d.page, "role": d.role, "reason": d.reason,
                 "expected": d.expected, "extracted": d.extracted,
                 "lost": d.lost, "rotation_applied": d.rotation_applied}
                for d in self.short_pages
            ],
        }


def build_row_ledger(page_metrics: list, target_pages: list, recognized_rows: int) -> RowLedger:
    """从页级指标汇总台账。每一个零产出/欠产出的页都必须带着 reason 出现在台账里。"""
    targets = set(target_pages or [])
    ledger = RowLedger(target_pages=len(targets), recognized_rows=recognized_rows)
    for m in page_metrics:
        if targets and m.page not in targets:
            continue
        # expected_rows 只在走 TableGrid 路径时才有值；html_fallback 页是 0，
        # 直接求和会得到一个比实际识别行数还小的分母（实测远东 expected=11 /
        # recognized=114 / dropped=0，22 行缺口一条没记账）。回落到 OCR 的 <tr>
        # 计数，保证分母始终是"这一页上确实存在多少行"。
        expected = m.expected_rows or m.row_count
        m = replace(m, expected_rows=expected) if expected != m.expected_rows else m
        ledger.expected_rows += expected
        if m.extracted_rows == 0 and m.expected_rows > 0:
            ledger.empty_pages.append(PageDrop(
                page=m.page, role=m.role, reason=m.fallback_reason or "no_rows_extracted",
                expected=m.expected_rows, extracted=0,
                rotation_applied=m.rotation_applied,
            ))
        elif m.extracted_rows < m.expected_rows:
            ledger.short_pages.append(PageDrop(
                page=m.page, role=m.role, reason=m.fallback_reason or "under_extracted",
                expected=m.expected_rows, extracted=m.extracted_rows,
                rotation_applied=m.rotation_applied,
            ))
    return ledger


# ─── Document quality report (§6) ─────────────────────────────────────────────

@dataclass
class QualityReport:
    """PASS / REVIEW / BLOCKED + per-metric breakdown (CLAUDE.md §6)."""

    status: str                     # PASS | REVIEW | BLOCKED
    blocking_reasons: list = field(default_factory=list)

    # Page coverage
    total_pages: int = 0
    rendered_pages: int = 0              # pages successfully rendered from PDF (= len(images))
    ocr_success_pages: int = 0           # pages where OCR returned non-empty HTML
    ocr_failed_pages: int = 0            # pages where OCR failed (empty HTML + failure record)
    ocr_failed_indices: list = field(default_factory=list)  # 1-based page numbers that failed OCR
    processed_pages: int = 0
    truncated: bool = False
    target_pages: list = field(default_factory=list)
    page_metrics: list = field(default_factory=list)  # list[PageMetric]

    # Row counts
    quote_line_count: int = 0
    subtotal_count: int = 0
    grand_total_count: int = 0

    # Field coverage
    source_ref_coverage: float = 0.0   # page/table/row present
    bbox_coverage: float = 0.0         # bbox present
    qty_parse_rate: float = 0.0
    price_parse_rate: float = 0.0
    arithmetic_consistency_rate: float = 0.0  # qty×unit_price ≈ total_price

    # Financial
    tax_basis_consistency: bool = True
    declared_total: float | None = None
    declared_total_diff: float | None = None

    # Sequence
    seq_missing: list = field(default_factory=list)
    seq_duplicate: list = field(default_factory=list)

    # Arithmetic mismatch gate (populated by _validate_arithmetic → compute_quality)
    arithmetic_mismatch_count: int = 0
    arithmetic_mismatch_amount: float = 0.0
    arithmetic_mismatch_ratio: float = 0.0
    arithmetic_mismatch_rows: list = field(default_factory=list)  # list[dict] per-row evidence

    # Failed target pages — target pages that failed processing (exception-level, not under-extracted)
    # Non-empty → quality BLOCKED; exposes replay cache misses as real test failures.
    failed_target_pages: list = field(default_factory=list)  # 1-based page numbers

    def to_dict(self) -> dict:
        """Serialise to plain dict for JSON responses / logging."""
        return {
            "status": self.status,
            "blocking_reasons": self.blocking_reasons,
            "total_pages": self.total_pages,
            "rendered_pages": self.rendered_pages,
            "ocr_success_pages": self.ocr_success_pages,
            "ocr_failed_pages": self.ocr_failed_pages,
            "ocr_failed_indices": self.ocr_failed_indices,
            "processed_pages": self.processed_pages,
            "truncated": self.truncated,
            "target_pages": self.target_pages,
            "quote_line_count": self.quote_line_count,
            "subtotal_count": self.subtotal_count,
            "grand_total_count": self.grand_total_count,
            "source_ref_coverage": self.source_ref_coverage,
            "bbox_coverage": self.bbox_coverage,
            "qty_parse_rate": self.qty_parse_rate,
            "price_parse_rate": self.price_parse_rate,
            "arithmetic_consistency_rate": self.arithmetic_consistency_rate,
            "tax_basis_consistency": self.tax_basis_consistency,
            "declared_total": self.declared_total,
            "declared_total_diff": self.declared_total_diff,
            "seq_missing": self.seq_missing,
            "seq_duplicate": self.seq_duplicate,
            "arithmetic_mismatch_count": self.arithmetic_mismatch_count,
            "arithmetic_mismatch_amount": self.arithmetic_mismatch_amount,
            "arithmetic_mismatch_ratio": self.arithmetic_mismatch_ratio,
            "arithmetic_mismatch_rows": self.arithmetic_mismatch_rows,
            "failed_target_pages": self.failed_target_pages,
        }


# ─── Top-level draft ─────────────────────────────────────────────────────────

@dataclass
class ExtractionDraft:
    """Full output of the recognizer — to be confirmed by the user before domain mapping."""
    doc_type: str                   # tender | quote
    source_file: str
    page_count: int
    processed_page_count: int
    target_pages: list              # 1-based page numbers of target tables
    rows: list                      # list[DraftRow] — official rows (pass quality gate)
    meta: dict                      # adapter-specific: brand info | supplier+declared_total
    quality: QualityReport
    reconcile: dict | None = None   # populated if xlsx_path provided
    # 召回页未通过合入门禁的行：隔离在此，**不进 rows / 不进比价 / 不入库**，
    # 仅供核对 UI 展示供用户人工裁决（§1.1 REVIEW：暴露难度、预填候选，不静默填值）。
    review_candidates: list = field(default_factory=list)  # list[DraftRow]
    # 行数守恒台账（doc/19 §L3）：应有 → 识别，逐页记录去向和原因。
    ledger: "RowLedger | None" = None


# ─── Quality gate logic (thresholds centralised here) ────────────────────────

_EXPECTED_ROWS_MIN_RATIO = 0.70     # trigger retry/tiling when extracted < expected * ratio
# _ARITHMETIC_PASS_THRESHOLD imported from domain_config.MATCH_ARITHMETIC_PASS_THRESHOLD
_DECLARED_TOTAL_DIFF_BLOCKED = 500.0  # yuan; above this → BLOCKED (without human note)
_DECLARED_TOTAL_DIFF_REVIEW = 50.0   # yuan; above this → REVIEW
_REVIEW_PAGE_RATIO = 0.30           # if > 30% target pages are under-extracted → BLOCKED
_ARITH_MISMATCH_BLOCKED_COUNT = 3        # ≥3 flagged rows → BLOCKED
_ARITH_MISMATCH_BLOCKED_RATIO = 0.02     # >2% of quote lines → BLOCKED
_ARITH_MISMATCH_BLOCKED_AMOUNT_RATIO = 0.10  # >10% of total amount → BLOCKED
# 识别阶段的单行算术容差：domain_config.EXTRACTION_ARITHMETIC_TOLERANCE（评审
# D5：此前是本文件内的模块级常量，未集中管理；值本身有意比入库门更宽，见该
# 常量定义处的注释，搬迁不改值）。
_ARITHMETIC_ROW_TOLERANCE = EXTRACTION_ARITHMETIC_TOLERANCE


def compute_quality(
    rows: list[DraftRow],
    page_metrics: list[PageMetric],
    total_pages: int,
    target_pages: list[int],
    declared_total: float | None = None,
    truncated: bool = False,
    *,
    rendered_pages: int = 0,
    ocr_success_pages: int = 0,
    ocr_failed_pages: int = 0,
    ocr_failed_indices: list[int] | None = None,
    failed_target_pages: list[int] | None = None,
) -> QualityReport:
    """Compute QualityReport from draft rows and page metrics."""
    n = len(rows)
    blocking: list[str] = []
    review_hints: list[str] = []

    # — Row type counts —
    quote_lines = [r for r in rows if r.row_type == DETAIL_ROW_TYPE]
    subtotals   = [r for r in rows if r.row_type == "subtotal"]
    totals      = [r for r in rows if r.row_type == "grand_total"]

    # — Pollution check —
    # grand_total rows must NOT appear as quote_line
    # (already filtered by row_type; this is just a sanity assertion)

    # — Source ref coverage —
    src_ok  = sum(1 for r in rows if r.source_ref.page > 0)
    bbox_ok = sum(1 for r in rows if r.source_ref.bbox is not None)

    # — Arithmetic consistency —
    # 口径与单行判据统一走 draft_integrity.check_row_arithmetic，全仓只此一份实现
    # （CLAUDE.md：同一业务结果不得各算各的）。它相对本处旧实现有两点改进：
    #   1. 单价与合价**按税基成对**取值，不再出现"不含税单价 vs 含税合价"这种比错尺子；
    #   2. 合价/(数量×单价) 落在简单倍数上的行判为报价口径（按根/按束报价），
    #      计入通过而非算术错误——倍率是报价方式的选择，不是错误。
    from apps.api.services.ingestion.draft_integrity import check_row_arithmetic

    arith_pass = 0
    arith_total = 0
    for r in quote_lines:
        res = check_row_arithmetic(r.fields, tolerance=_ARITHMETIC_ROW_TOLERANCE)
        if res.status == "not_evaluable":
            continue
        arith_total += 1
        if res.status in ("ok", "multiplier"):
            arith_pass += 1

    arith_rate = round(arith_pass / arith_total, 3) if arith_total > 0 else 1.0

    # — Missing-qty rows (design/26 §9)：not_evaluable 行在上面的循环里被 continue
    # 跳过，只是不计入算术自洽的分母——分母不虚高是对的，但这些行本身就此从
    # blocking/review_hints 里彻底消失，界面上看起来"跟没发生一样"。qty 是
    # 唯一在这轮门槛决策里保留 96% 硬指标的字段（qty×单价=合价，误差会传导到
    # 评标总价），"读不出数量"不能是无声的——跟 `not_quoted`（原文明确不报价，
    # 合法）分开统计，只数"该有数但读不出"这一类。
    qty_missing_count = sum(
        1 for r in quote_lines
        if r.fields.get("qty") is None and not r.fields.get("not_quoted")
    )

    # — Tax basis consistency —
    tax_bases = set()
    for r in quote_lines:
        f = r.fields
        has_incl = f.get("unit_price_incl_tax") or f.get("total_price_incl_tax")
        has_excl = f.get("unit_price_excl_tax") or f.get("total_price_excl_tax")
        if has_incl and not has_excl:
            tax_bases.add("incl_only")
        elif has_excl and not has_incl:
            tax_bases.add("excl_only")
        elif has_incl and has_excl:
            tax_bases.add("both")
    tax_consistent = len(tax_bases) <= 1

    # — Declared total diff —
    declared_diff: float | None = None
    if declared_total is not None:
        line_sum = 0.0
        for r in quote_lines:
            f = r.fields
            try:
                t = float(
                    f.get("total_price_incl_tax")
                    or f.get("total_price")
                    or 0
                )
                line_sum += t
            except (TypeError, ValueError):
                pass
        if line_sum > 0:
            declared_diff = round(abs(line_sum - declared_total), 2)

    # — Sequence gaps —
    seqs = []
    for r in quote_lines:
        s = str(r.fields.get("seq") or "").strip()
        if s.isdigit():
            seqs.append(int(s))
    seq_set = set(seqs)
    seq_missing: list[str] = []
    seq_dup: list[str] = []
    if seqs:
        full_range = set(range(min(seqs), max(seqs) + 1))
        seq_missing = sorted(str(s) for s in (full_range - seq_set))
    seq_counts: dict[int, int] = {}
    for s in seqs:
        seq_counts[s] = seq_counts.get(s, 0) + 1
    seq_dup = [str(s) for s, c in seq_counts.items() if c > 1]

    # — Page under-extraction ratio —
    under_pages = [
        m for m in page_metrics
        if m.expected_rows > 0
        and m.extracted_rows < m.expected_rows * _EXPECTED_ROWS_MIN_RATIO
    ]

    # — Arithmetic mismatch gate (rows flagged by _validate_arithmetic) —
    mismatch_rows_info: list[dict] = []
    total_line_amount = 0.0
    for r in quote_lines:
        try:
            total_line_amount += float(
                r.fields.get("total_price_incl_tax") or r.fields.get("total_price") or 0
            )
        except (TypeError, ValueError):
            pass
    for r in quote_lines:
        if "qty_arithmetic_mismatch" not in r.validation_flags:
            continue
        f = r.fields
        try:
            stated = float(f.get("arith_actual_total") or 0)
        except (TypeError, ValueError):
            stated = 0.0
        has_evidence = (
            r.source_ref.bbox is not None
            or r.source_ref.table > 0
            or r.source_ref.row > 0
        )
        mismatch_rows_info.append({
            "page": r.source_ref.page,
            **({"page_end": r.source_ref.page_end}
               if r.source_ref.page_end is not None
               and r.source_ref.page_end > r.source_ref.page else {}),
            "table": r.source_ref.table,
            "row": r.source_ref.row,
            "seq": f.get("seq"),
            "qty": f.get("qty"),
            "unit_price": (
                f.get("unit_price_incl_tax")
                or f.get("unit_price_excl_tax")
                or f.get("unit_price")
            ),
            "stated_total": f.get("arith_actual_total"),
            "calculated_total": f.get("arith_expected_total"),
            "suggested_qty": f.get("arith_suggested_qty"),
            "_has_evidence": has_evidence,
            "_stated_amount": stated,
        })
    mismatch_count = len(mismatch_rows_info)
    mismatch_amount = round(sum(r["_stated_amount"] for r in mismatch_rows_info), 2)
    mismatch_ratio = round(mismatch_count / len(quote_lines), 4) if quote_lines else 0.0
    # strip private keys before returning
    arith_report_rows = [{k: v for k, v in r.items() if not k.startswith("_")}
                         for r in mismatch_rows_info]

    _failed_target = sorted(failed_target_pages or [])

    # ── Blocking conditions ────────────────────────────────────────────────
    if _failed_target:
        # Any target page that raised an exception (e.g. snapshot cache miss) → BLOCKED.
        # Prevents silently passing tests when pages 5-9 fail in replay mode.
        blocking.append(f"failed_target_pages={_failed_target}")
    if truncated:
        blocking.append("document_truncated")
    if n > 0 and len(quote_lines) == 0:
        blocking.append("zero_quote_lines_with_data")
    if declared_diff is not None and declared_diff > _DECLARED_TOTAL_DIFF_BLOCKED:
        blocking.append(f"declared_total_diff={declared_diff:.2f}")
    if target_pages and len(under_pages) / len(target_pages) > _REVIEW_PAGE_RATIO:
        blocking.append(f"under_extracted_pages={len(under_pages)}/{len(target_pages)}")

    # Arithmetic mismatch: any flagged row → at least REVIEW; BLOCKED if severe
    if mismatch_count > 0:
        _no_evidence = any(not r["_has_evidence"] for r in mismatch_rows_info)
        _high_amount = (
            total_line_amount > 0
            and mismatch_amount / total_line_amount > _ARITH_MISMATCH_BLOCKED_AMOUNT_RATIO
        )
        _high_count = (
            mismatch_count >= _ARITH_MISMATCH_BLOCKED_COUNT
            or mismatch_ratio > _ARITH_MISMATCH_BLOCKED_RATIO
        )
        _total_also_bad = (
            declared_diff is not None and declared_diff > _DECLARED_TOTAL_DIFF_REVIEW
        )
        if _no_evidence or _high_amount or _high_count or _total_also_bad:
            blocking.append(f"qty_arithmetic_mismatch_blocked={mismatch_count}")
        else:
            # will fall into review_hints below
            pass

    # ── Review conditions ─────────────────────────────────────────────────
    if mismatch_count > 0 and f"qty_arithmetic_mismatch_blocked={mismatch_count}" not in blocking:
        review_hints.append(f"qty_arithmetic_mismatch={mismatch_count}")
    if arith_rate < _ARITHMETIC_PASS_THRESHOLD and arith_total > 0:
        review_hints.append(f"arithmetic_consistency={arith_rate:.2f}")
    if not tax_consistent:
        review_hints.append("tax_basis_inconsistent")
    if declared_diff is not None and declared_diff > _DECLARED_TOTAL_DIFF_REVIEW:
        review_hints.append(f"declared_total_diff={declared_diff:.2f}")
    if n > 0 and src_ok / n < 1.0:
        review_hints.append(f"source_ref_coverage={src_ok/n:.2f}")
    if seq_missing:
        review_hints.append(f"seq_missing={seq_missing}")
    if under_pages:
        review_hints.append(f"under_extracted_pages={[m.page for m in under_pages]}")
    # bbox 缺失：§5/§14 要求每条确认行可逐行定位；bbox=0 不得 PASS
    if len(quote_lines) > 0 and bbox_ok == 0:
        review_hints.append("bbox_coverage=0 (no row-level localization)")
    # 无序号行：抽出但无法用序号锚定，需人工核对身份
    no_seq_count = sum(
        1 for r in quote_lines
        if not str(r.fields.get("seq") or "").strip().isdigit()
    )
    if no_seq_count > 0:
        review_hints.append(f"no_seq_rows={no_seq_count}")
    if qty_missing_count > 0:
        review_hints.append(f"qty_missing_rows={qty_missing_count}")

    if blocking:
        status = "BLOCKED"
        all_reasons = blocking
    elif review_hints:
        status = "REVIEW"
        all_reasons = review_hints
    else:
        status = "PASS"
        all_reasons = []

    return QualityReport(
        status=status,
        blocking_reasons=all_reasons,
        total_pages=total_pages,
        rendered_pages=rendered_pages,
        ocr_success_pages=ocr_success_pages,
        ocr_failed_pages=ocr_failed_pages,
        ocr_failed_indices=ocr_failed_indices or [],
        processed_pages=len(page_metrics),
        truncated=truncated,
        target_pages=target_pages,
        page_metrics=page_metrics,
        quote_line_count=len(quote_lines),
        subtotal_count=len(subtotals),
        grand_total_count=len(totals),
        source_ref_coverage=round(src_ok / n, 3) if n > 0 else 0.0,
        bbox_coverage=round(bbox_ok / n, 3) if n > 0 else 0.0,
        qty_parse_rate=round(
            sum(1 for r in quote_lines if r.fields.get("qty") is not None)
            / max(1, len(quote_lines)), 3
        ),
        price_parse_rate=round(
            sum(1 for r in quote_lines
                if r.fields.get("unit_price_excl_tax") or r.fields.get("unit_price"))
            / max(1, len(quote_lines)), 3
        ),
        arithmetic_consistency_rate=arith_rate,
        tax_basis_consistency=tax_consistent,
        declared_total=declared_total,
        declared_total_diff=declared_diff,
        seq_missing=seq_missing,
        seq_duplicate=seq_dup,
        arithmetic_mismatch_count=mismatch_count,
        arithmetic_mismatch_amount=mismatch_amount,
        arithmetic_mismatch_ratio=mismatch_ratio,
        arithmetic_mismatch_rows=arith_report_rows,
        failed_target_pages=_failed_target,
    )
