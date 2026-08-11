"""招标文件驱动的评标：同规格基准 + 评标总价 + 三态门禁 + pending分级 回归测试。

锁定（用户2026-06-21规则）：
- 同规格基准键 family+DN+PN+unit+tax_basis；PN/tax_basis 不同不混样；样本<5或缺PN→None（不计异常）；
- 偏差相对同规格中位数（非地板价/P10）；
- evaluated_total 恒用 tender_qty×含税单价（供应商报价数量仅校验）；
- pending 分级：仅数量来源冲突且口径可得→纳入(quantity_source_conflict)；价/口径/对齐未确认→未决不静默排除；
- checksum：fail 才阻断，unknown 不阻断；
- 三态 recommendation_level，blocked 也产出可供AI解释的上下文；
- EvaluationPolicy：合理低价/单一授标/无权重/需委员会，不可自动定标。
"""
from __future__ import annotations

from apps.api.services.history.comparison import spec_baseline_from_index
from apps.api.services.matrix.bid_matrix import _evaluate_cell, _compute_recommendation
from apps.api.services.matrix.bid_insight import _build_matrix_text
from apps.api.services.matrix.evaluation_policy import DEFAULT_EVALUATION_POLICY as POLICY

_THR = {"yellow": 0.05, "red": 0.10}


# ── 同规格基准 ────────────────────────────────────────────────────────────────
def _idx(n=5):
    return {("止回阀", "DN50", "PN16", "个", "incl_tax"): [100.0] * n}


def test_spec_baseline_median_full_key():
    idx = {("止回阀", "DN50", "PN16", "个", "incl_tax"): [100, 110, 120, 130, 140]}
    bl = spec_baseline_from_index(idx, "止回阀", "DN50", "PN16", "个", "incl_tax")
    assert bl and bl["median"] == 120 and bl["count"] == 5


def test_spec_baseline_pn_not_mixed():
    """family/DN/unit 相同但 PN 不同 → 不同桶 → 无基准（不得混样）。"""
    assert spec_baseline_from_index(_idx(), "止回阀", "DN50", "PN25", "个", "incl_tax") is None


def test_spec_baseline_tax_basis_not_mixed():
    """tax_basis 不同 → 不同桶 → 无基准（含税/不含税不混样）。"""
    assert spec_baseline_from_index(_idx(), "止回阀", "DN50", "PN16", "个", "excl_tax") is None


def test_spec_baseline_missing_pn_no_baseline():
    """缺 PN（招标清单常见）→ 规格键不全 → None，不产生异常。"""
    assert spec_baseline_from_index(_idx(), "止回阀", "DN50", None, "个", "incl_tax") is None


def test_spec_baseline_insufficient_samples():
    """样本<5 → None（无可靠同规格基准）。"""
    assert spec_baseline_from_index(_idx(4), "止回阀", "DN50", "PN16", "个", "incl_tax") is None


# ── 评标单元格：tender_qty × 含税单价；pending 分级 ─────────────────────────────
def _cell(**kw):
    base = {
        "cell_status": "quoted", "price": None, "price_basis": None, "incl_unit": None,
        "supplier_qty": None, "unit": "个", "item_canonical": None,
    }
    base.update(kw)
    return base


def test_evaluated_uses_tender_qty_not_supplier_qty():
    """凯硕row89 型：报价数量1(OCR误) 但 tender_qty=4、含税单价865 闭环 → 纳入评标，金额=4×865。"""
    c = _cell(cell_status="quoted", price=865, price_basis="dual_tax", incl_unit=865,
              supplier_qty=1, item_canonical={"valve_type": "球阀", "dn": "DN100"})
    _evaluate_cell(c, anchor_qty=4, fam="球阀", dn="DN100", pn=None, a_unit="个",
                   spec_index={}, thresholds=_THR)
    assert c["eval_amount"] == 3460          # 4×865，不是 1×865
    assert c["evaluable"] is True
    assert c["eval_status"] == "quantity_source_conflict"


def test_pending_qty_only_included():
    """pending 但仅数量来源冲突（DN/族/单位一致，含税可得）→ 纳入评标 + 标记。"""
    c = _cell(cell_status="pending", price=865, price_basis="dual_tax", incl_unit=865,
              supplier_qty=1, item_canonical={"valve_type": "止回阀", "dn": "DN100"})
    _evaluate_cell(c, 4, "止回阀", "DN100", None, "个", {}, _THR)
    assert c["evaluable"] and c["eval_status"] == "quantity_source_conflict" and c["eval_amount"] == 3460


def test_pending_dn_conflict_undecided_not_silently_dropped():
    """pending 且 DN 不符（非数量原因）→ 未决，不纳入完整评标（也不静默排除）。"""
    c = _cell(cell_status="pending", price=865, price_basis="dual_tax", incl_unit=865,
              supplier_qty=4, item_canonical={"valve_type": "球阀", "dn": "DN80"})
    _evaluate_cell(c, 4, "球阀", "DN100", None, "个", {}, _THR)
    assert c["evaluable"] is False and c["eval_status"] == "alignment_pending"


def test_pending_qty_only_prefix_family_variant_included():
    """凯硕row89型：cell valve_type='缓闭式止回阀'（含前缀），锚点族='止回阀'。
    族归一化须与锚点同管线（extract→normalize），不得因前缀误判为对齐未决。
    DN100/个 一致 + 含税可得 → quantity_source_conflict 纳入（4×865=3460）。"""
    c = _cell(cell_status="pending", price=865, price_basis="dual_tax", incl_unit=865,
              supplier_qty=1, item_canonical={"valve_type": "缓闭式止回阀", "dn": "DN100"})
    _evaluate_cell(c, 4, "止回阀", "DN100", None, "个", {}, _THR)
    assert c["evaluable"] is True
    assert c["eval_status"] == "quantity_source_conflict"
    assert c["eval_amount"] == 3460


def test_excl_tax_undecided():
    """excl_tax（不含税，无含税口径）→ 不纳入完整评标总价（未决，不静默与含税混比）。"""
    c = _cell(cell_status="quoted", price=100, price_basis="excl_tax", incl_unit=None,
              supplier_qty=1, item_canonical={})
    _evaluate_cell(c, 1, "球阀", "DN100", None, "个", {}, _THR)
    assert c["evaluable"] is False and c["eval_status"] == "basis_unconfirmed"


def test_unspecified_single_column_included_as_incl_tax():
    """锦存型：单一价格列 price_basis=unspecified，incl_unit 可得（=该唯一价）→
    按招标含税单价要求纳入评标（1×93=93），标 tax_basis_assumed（假定非确认）。"""
    c = _cell(cell_status="quoted", price=93, price_basis="unspecified", incl_unit=93,
              supplier_qty=1, item_canonical={})
    _evaluate_cell(c, 1, "球阀", "DN100", None, "个", {}, _THR)
    assert c["evaluable"] is True
    assert c["eval_amount"] == 93
    assert c["eval_status"] == "ok"
    assert c["tax_basis_assumed"] is True


def test_unspecified_assumed_not_basis_confirmed_but_ranked():
    """单一价格列假定含税的供应商：纳入排名（eligible），但 basis_confirmed=False，
    且产出 tax_assumed_lines 风险提示（诚实标注假定，不冒充确认）。"""
    rows = [_row({"supplier_id": 1, "evaluable": True, "eval_status": "ok",
                  "eval_amount": 93, "alert_level": "normal", "tender_qty": 1,
                  "incl_unit": 93, "price": 93, "tax_basis_assumed": True})]
    rec = _compute_recommendation(rows, [1], _LABELS[:1], 1, {1: "unknown"}, POLICY)
    se = rec["supplier_evaluation"][0]
    assert se["eligible_for_ranking"] is True
    assert se["tax_assumed_lines"] == 1
    assert se["basis_confirmed"] is False
    assert any("税口径假定含税" in r for r in rec["risks"])


def test_deviation_vs_spec_median():
    """偏差相对同规格中位数：150 vs 100 = +50% → red。"""
    c = _cell(cell_status="quoted", price=150, price_basis="incl_tax", incl_unit=150,
              supplier_qty=1, item_canonical={})
    _evaluate_cell(c, 1, "止回阀", "DN50", "PN16", "个", _idx(), _THR)
    assert c["deviation_pct"] == 0.5 and c["alert_level"] == "red"


def test_no_spec_baseline_no_anomaly():
    """无同规格基准（空索引）→ deviation=null，alert=normal（不产生假异常）。"""
    c = _cell(cell_status="quoted", price=3171, price_basis="incl_tax", incl_unit=3171,
              supplier_qty=1, item_canonical={})
    _evaluate_cell(c, 1, "球阀", "DN100", None, "个", {}, _THR)
    assert c["deviation_pct"] is None and c["alert_level"] == "normal"


# ── 推荐：确定性排名 + 三态 + checksum 语义 ───────────────────────────────────
def _row(*cells):
    return {"suppliers": list(cells)}


def _ev(sid, evaluable, amount, status="ok", alert="normal"):
    return {"supplier_id": sid, "evaluable": evaluable, "eval_amount": amount,
            "eval_status": status, "alert_level": alert, "tender_qty": 1,
            "incl_unit": amount, "price": amount}


_LABELS = [{"id": 1, "name": "A", "letter": "A"}, {"id": 2, "name": "B", "letter": "B"}]


def test_price_ranking_matches_evaluated_total():
    """价格优选==评标总价排名（A 200 < B 240）。checksum unknown 不阻断 → conditional。"""
    rows = [_row(_ev(1, True, 100), _ev(2, True, 120)),
            _row(_ev(1, True, 100), _ev(2, True, 120))]
    rec = _compute_recommendation(rows, [1, 2], _LABELS, 2, {1: "unknown", 2: "unknown"}, POLICY)
    assert rec["recommendation_level"] == "conditional"
    assert [r["supplier_id"] for r in rec["price_ranking"]] == [1, 2]
    assert rec["price_preferred_candidate"]["supplier_id"] == 1
    assert rec["supplier_evaluation"][0]["evaluated_total"] == 200


def test_checksum_unknown_not_blocking():
    rows = [_row(_ev(1, True, 100))]
    rec = _compute_recommendation(rows, [1], _LABELS[:1], 1, {1: "unknown"}, POLICY)
    assert rec["recommendation_level"] == "conditional"


def test_checksum_fail_excludes_from_ranking():
    """checksum fail → 不入排名；若唯一供应商失格 → blocked。"""
    rows = [_row(_ev(1, True, 100))]
    rec = _compute_recommendation(rows, [1], _LABELS[:1], 1, {1: "fail"}, POLICY)
    assert rec["recommendation_level"] == "blocked"
    assert rec["price_preferred_candidate"] is None


def test_undecided_supplier_not_eligible_but_shown():
    """税口径未确认的供应商 → 不入排名，但 supplier_evaluation 仍展示（未决金额）。"""
    rows = [_row(_ev(1, True, 100), {"supplier_id": 2, "evaluable": False,
                 "eval_status": "basis_unconfirmed", "alert_level": "normal",
                 "tender_qty": 2, "incl_unit": None, "price": 50})]
    rec = _compute_recommendation(rows, [1, 2], _LABELS, 1, {1: "unknown", 2: "unknown"}, POLICY)
    se = {s["supplier_id"]: s for s in rec["supplier_evaluation"]}
    assert se[2]["eligible_for_ranking"] is False
    assert se[2]["undecided_lines"] == 1 and se[2]["undecided_amount"] == 100  # 2×50
    assert [r["supplier_id"] for r in rec["price_ranking"]] == [1]


def test_blocked_still_has_ai_context():
    """blocked 时仍产出可供 AI 解释的上下文（policy/风险/非价格因素），prompt 不崩。"""
    rows = [_row({"supplier_id": 1, "evaluable": False, "eval_status": "basis_unconfirmed",
                  "alert_level": "normal", "tender_qty": 1, "incl_unit": None, "price": 10})]
    rec = _compute_recommendation(rows, [1], _LABELS[:1], 1, {1: "unknown"}, POLICY)
    assert rec["recommendation_level"] == "blocked"
    blocks = _build_matrix_text({"suppliers": _LABELS[:1], "rows": rows, **rec})
    assert "合理低价" in blocks["policy_text"]
    assert blocks["ranking_text"]  # 非空（"无投标人..."）


# ── EvaluationPolicy ─────────────────────────────────────────────────────────
def test_policy_no_auto_award():
    assert POLICY.lowest_price_wins is False
    assert POLICY.award_mode == "single_supplier"
    assert POLICY.allows_split_award is False
    assert POLICY.can_auto_declare_winner is False
    assert POLICY.weights is None
    assert POLICY.final_decision_requires_committee is True
