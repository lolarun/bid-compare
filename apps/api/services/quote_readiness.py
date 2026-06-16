"""QuoteReadiness — per-supplier auto-matrix admission assessment.

Takes match statistics (from import_and_match per_supplier_stats) and optional
document metadata (from OCR cover extraction) and computes whether a supplier's
quotes can automatically enter the bid matrix.

auto_matrix_ready = True means: the confirmed portion can enter the matrix.
It does NOT mean the entire quote is anomaly-free — has_exclusions=True means
some rows were excluded (pending/residue/validation_failed).
"""

from __future__ import annotations

from dataclasses import dataclass, field


_CHECKSUM_TOLERANCE = 0.05  # 5% tolerance for bid_total vs computed_total


@dataclass
class QuoteReadiness:
    supplier_id: int
    supplier_name: str
    # Row counts
    quote_rows: int = 0
    valid_rows: int = 0
    matched_rows: int = 0
    pending_rows: int = 0
    residue_rows: int = 0
    validation_failed_rows: int = 0
    # Checksum
    doc_total: float | None = None
    computed_total: float | None = None
    bid_total_basis: str = "unknown"   # "tax_included" | "tax_excluded" | "unknown"
    checksum_status: str = "unknown"   # "passed" | "failed" | "unknown" | "basis_mismatch"
    # Conflict
    cross_type_conflicts: int = 0
    # Decision
    auto_matrix_ready: bool = False
    has_exclusions: bool = False        # True if pending/residue/validation_failed > 0
    matrix_scope: str = "confirmed_only"
    excluded_rows: dict = field(default_factory=dict)  # {pending, residue, validation_failed}
    warnings: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "supplier_id": self.supplier_id,
            "supplier_name": self.supplier_name,
            "quote_rows": self.quote_rows,
            "valid_rows": self.valid_rows,
            "matched_rows": self.matched_rows,
            "pending_rows": self.pending_rows,
            "residue_rows": self.residue_rows,
            "validation_failed_rows": self.validation_failed_rows,
            "doc_total": self.doc_total,
            "computed_total": self.computed_total,
            "bid_total_basis": self.bid_total_basis,
            "checksum_status": self.checksum_status,
            "cross_type_conflicts": self.cross_type_conflicts,
            "auto_matrix_ready": self.auto_matrix_ready,
            "has_exclusions": self.has_exclusions,
            "matrix_scope": self.matrix_scope,
            "excluded_rows": self.excluded_rows,
            "warnings": self.warnings,
            "reasons": self.reasons,
        }


def assess_readiness(
    supplier_id: int,
    supplier_name: str,
    stats: dict,
    doc_meta: dict | None = None,
) -> QuoteReadiness:
    """Compute QuoteReadiness for one supplier.

    Args:
        supplier_id: supplier PK
        supplier_name: display name
        stats: dict from per_supplier_stats[supplier_id] (output of import_and_match)
              keys: quote_rows, matched_rows, pending_rows, residue_rows,
                    aggregated_rows, computed_total (optional)
        doc_meta: dict from OCR cover extraction, keys: bid_total, bid_total_basis, tax_rate
    """
    quote_rows = stats.get("quote_rows", 0)
    matched_rows = stats.get("matched_rows", 0)
    pending_rows = stats.get("pending_rows", 0)
    residue_rows = stats.get("residue_rows", 0)
    validation_failed_rows = stats.get("validation_failed_rows", 0)
    computed_total = stats.get("computed_total")
    cross_type_conflicts = stats.get("cross_type_conflicts", 0)

    valid_rows = quote_rows - validation_failed_rows

    doc_total: float | None = None
    bid_total_basis = "unknown"
    if doc_meta:
        doc_total = doc_meta.get("bid_total")
        bid_total_basis = doc_meta.get("bid_total_basis") or "unknown"

    # Checksum
    checksum_status = _compute_checksum(doc_total, computed_total, bid_total_basis)

    # Exclusions
    excluded_rows = {
        "pending": pending_rows,
        "residue": residue_rows,
        "validation_failed": validation_failed_rows,
    }
    has_exclusions = (pending_rows + residue_rows + validation_failed_rows) > 0

    # Admission decision
    warnings: list[str] = []
    reasons: list[str] = []

    auto_matrix_ready = True

    if checksum_status == "failed":
        auto_matrix_ready = False
        reasons.append(f"封面总价与行级合计不符 (doc={doc_total}, computed={computed_total:.2f})")

    if cross_type_conflicts > 0:
        auto_matrix_ready = False
        reasons.append(f"发现 {cross_type_conflicts} 条类型冲突匹配，需人工核查")

    if has_exclusions:
        if pending_rows > 0:
            warnings.append(f"{pending_rows} 条待确认行已排除在矩阵外")
        if residue_rows > 0:
            warnings.append(f"{residue_rows} 条未匹配行已排除在矩阵外")
        if validation_failed_rows > 0:
            warnings.append(f"{validation_failed_rows} 条金额异常行已排除在矩阵外")

    # Ratio diagnostics
    if quote_rows > 0:
        matched_ratio = matched_rows / max(valid_rows, 1)
        if matched_ratio < 0.70:
            warnings.append(
                f"匹配率偏低: {matched_rows}/{valid_rows} 行进入矩阵 ({matched_ratio:.0%}), "
                "建议检查报价品名与招标清单是否对应"
            )

    if quote_rows == 0:
        auto_matrix_ready = False
        reasons.append("无报价行")

    return QuoteReadiness(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        quote_rows=quote_rows,
        valid_rows=valid_rows,
        matched_rows=matched_rows,
        pending_rows=pending_rows,
        residue_rows=residue_rows,
        validation_failed_rows=validation_failed_rows,
        doc_total=doc_total,
        computed_total=computed_total,
        bid_total_basis=bid_total_basis,
        checksum_status=checksum_status,
        cross_type_conflicts=cross_type_conflicts,
        auto_matrix_ready=auto_matrix_ready,
        has_exclusions=has_exclusions,
        matrix_scope="confirmed_only",
        excluded_rows=excluded_rows,
        warnings=warnings,
        reasons=reasons,
    )


def _compute_checksum(
    doc_total: float | None,
    computed_total: float | None,
    bid_total_basis: str,
) -> str:
    if doc_total is None or computed_total is None:
        return "unknown"
    if bid_total_basis == "tax_excluded":
        # We can't reliably compare excl-tax total vs incl-tax computed sum
        return "basis_mismatch"
    if doc_total <= 0:
        return "unknown"
    diff_ratio = abs(doc_total - computed_total) / doc_total
    return "passed" if diff_ratio <= _CHECKSUM_TOLERANCE else "failed"
