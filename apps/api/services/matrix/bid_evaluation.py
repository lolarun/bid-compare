"""bid_evaluation.py — 单格评标逻辑（§10.3 拆分自 bid_matrix.py）。

包含：
  _anchor_spec      — 招标锚点规格键
  _canon_family     — 规格族归一化（与锚点侧同管线）
  _pending_is_qty_only — pending 冲突是否仅数量来源
  _evaluate_cell    — 就地填充同规格偏差 + 评标资格字段

这些函数由 bid_matrix.py 内部调用，也可由 build_anchor_review_matrix 调用。
"""
from __future__ import annotations

from apps.api.core.domain_config import SEQ_QTY_TOLERANCE
from apps.api.core.enums import (
    CELL_MISSING, CELL_EXCLUDED, CELL_PENDING,
)
from apps.api.services.ingestion.canonical import extract_valve_canonical, normalize_valve_family
from apps.api.services.history.comparison import spec_baseline_from_index, determine_alert

# 与 anchor_match.py 共用同一个数量比较容差（评审 D4：此前三处各自定义
# 0.001/0.001/1e-6，1e-6 那处会把另两处判齐的行判成冲突）。
_EVAL_QTY_TOL = SEQ_QTY_TOLERANCE


def _anchor_spec(anchor) -> tuple:
    """(family, dn, pn, unit) — 招标锚点的规格键（同规格基准/偏差以招标侧为准）。"""
    c = extract_valve_canonical(getattr(anchor, "name", "") or "", getattr(anchor, "spec", "") or "")
    fam = normalize_valve_family(c.get("valve_type"))
    return fam, c.get("dn"), c.get("pn"), (getattr(anchor, "unit", "") or "").strip()


def _canon_family(vt: str | None) -> str | None:
    """从原始阀型串归一化出族 — 与 _anchor_spec 同管线（extract_valve_canonical→normalize）。

    必须与锚点侧一致：normalize_valve_family('缓闭式止回阀')='缓闭式止回阀'（不降族），
    而 extract_valve_canonical 先抽出基础族'止回阀'。直接 normalize 存储 canonical 会
    与锚点族不对称，导致 quantity_source_conflict 被误判为 alignment_pending。
    """
    if not vt:
        return None
    return normalize_valve_family(extract_valve_canonical(vt, "").get("valve_type"))


def _pending_is_qty_only(cell: dict, fam, dn, a_unit) -> bool:
    """pending 单元格的冲突是否**仅数量来源**（DN/族/单位一致，价格口径可得）。

    仅当如此才允许纳入评标总价（标 quantity_source_conflict）；否则属对齐未决，不纳入。
    """
    c = cell.get("item_canonical") or {}
    c_dn = c.get("dn")
    c_fam = _canon_family(c.get("valve_type"))
    if dn and c_dn and dn != c_dn:
        return False
    if fam and c_fam and fam != c_fam:
        return False
    cu = (cell.get("unit") or "").strip()
    if a_unit and cu and a_unit != cu:
        return False
    return True


def _evaluate_cell(cell: dict, anchor_qty, fam, dn, pn, a_unit,
                   spec_index: dict, thresholds: dict) -> None:
    """就地填充单元格的同规格偏差 + 评标资格字段。

    评标金额恒 = 招标数量 × 含税单价（incl_unit）；供应商报价数量仅作校验。
    偏差相对同规格中位数（展示==计算）；无可靠同规格基准 → deviation=null、不计异常。
    """
    cell["tender_qty"] = anchor_qty
    cell["eval_amount"] = None
    cell["evaluable"] = False
    cell["baseline"] = None
    cell["tax_basis_assumed"] = False
    status = cell["cell_status"]
    basis = cell.get("price_basis")
    incl_unit = cell.get("incl_unit")

    if status in (CELL_MISSING, CELL_EXCLUDED) or cell.get("price") is None:
        cell["eval_status"] = "missing" if status == CELL_MISSING else status
        cell["deviation_pct"] = None
        cell["alert_level"] = "normal"
        return

    # ── 同规格偏差：按本格税口径取对应桶；展示值与计算值同源（中位数）──
    # unspecified（单一价格列）按招标含税要求与含税桶比较。
    bl = None
    cmp_price = None
    if incl_unit is not None and basis in ("incl_tax", "dual_tax", "unspecified"):
        bl = spec_baseline_from_index(spec_index, fam, dn, pn, a_unit, "incl_tax")
        cmp_price = incl_unit
    elif basis == "excl_tax" and cell.get("price"):
        bl = spec_baseline_from_index(spec_index, fam, dn, pn, a_unit, "excl_tax")
        cmp_price = cell["price"]
    if bl and cmp_price:
        dev = round((cmp_price - bl["median"]) / bl["median"], 4)
        cell["deviation_pct"] = dev
        cell["alert_level"] = determine_alert(dev, thresholds)
        cell["baseline"] = bl
    else:
        cell["deviation_pct"] = None
        cell["alert_level"] = "normal"   # 无可靠同规格基准 → 不计异常

    # ── 评标资格：必须有可纳入的含税口径单价 ──
    if incl_unit is None:
        cell["eval_status"] = "basis_unconfirmed"   # 税口径未确认（excl_tax/unknown）→ 未决（不静默排除）
        return
    # 单一价格列按招标含税要求纳入，但标记假定（非确认），供风险提示与人工核实。
    cell["tax_basis_assumed"] = (basis == "unspecified")
    eval_amount = round(float(anchor_qty) * incl_unit, 2) if anchor_qty else None
    sq = cell.get("supplier_qty")
    qty_conflict = (
        sq is not None and anchor_qty is not None
        and abs(float(sq) - float(anchor_qty)) > _EVAL_QTY_TOL
    )
    if status == CELL_PENDING:
        if _pending_is_qty_only(cell, fam, dn, a_unit):
            cell["eval_status"] = "quantity_source_conflict"
            cell["evaluable"] = True
            cell["eval_amount"] = eval_amount
        else:
            cell["eval_status"] = "alignment_pending"   # 非数量原因未决 → 不纳入
        return
    # align / aggregated
    cell["evaluable"] = True
    cell["eval_amount"] = eval_amount
    cell["eval_status"] = "quantity_source_conflict" if qty_conflict else "ok"
