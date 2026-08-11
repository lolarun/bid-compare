"""v2.4 pipeline unit tests (24 tests, zero real API calls).

Tests cover:
  - canonical.py: extract_valve_canonical + canonical_match_score (tests 1-8)
  - pipeline._validate_items (tests 9-11)
  - page_classifier.classify_page (tests 12-18, including HTML fixtures)
  - anchor_match.match_anchors canonical hard-filter (tests 19-21)
  - quote_readiness.assess_readiness (tests 22-24)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ─────────────────────────────────────────────────────────────────────────────
#  1-8: canonical.py
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.services.canonical import canonical_match_score, extract_valve_canonical


def test_canonical_basic():
    c = extract_valve_canonical("截止阀", "DN25 PN16 不锈钢 螺纹")
    assert c["valve_type"] == "截止阀"
    assert c["dn"] == "DN25"
    assert c["pn"] == "PN16"
    assert c["material"] == "不锈钢"
    assert c["connection"] == "螺纹"


def test_canonical_dn_normalize():
    # Φ57 → DN50 (via OD→DN map: OD 57mm ≈ DN50)
    c = extract_valve_canonical("截止阀", "Φ57 PN16")
    assert c["dn"] == "DN50"

    # 2寸 → DN50 (inch conversion: 2" = DN50)
    c2 = extract_valve_canonical("球阀", "2寸 PN10")
    assert c2["dn"] == "DN50"

    # Explicit DN still parsed correctly
    c3 = extract_valve_canonical("蝶阀", "DN100 PN10")
    assert c3["dn"] == "DN100"


def test_canonical_valve_order():
    # "Y型过滤器" must win over the shorter suffix "过滤器"
    c = extract_valve_canonical("Y型过滤器", "DN25 PN16")
    assert c["valve_type"] == "Y型过滤器"


def test_canonical_score_valve_conflict():
    a = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    q = {"valve_type": "球阀", "dn": "DN25", "pn": "PN16"}
    assert canonical_match_score(a, q) == 0.0


def test_canonical_score_dn_conflict():
    a = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    q = {"valve_type": "截止阀", "dn": "DN32", "pn": "PN16"}
    assert canonical_match_score(a, q) == 0.0


def test_canonical_score_pn_conflict():
    a = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    q = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN25"}
    assert canonical_match_score(a, q) == 0.0


def test_canonical_score_full_match():
    a = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    q = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    assert canonical_match_score(a, q) == 1.0


def test_canonical_score_wildcard():
    # Anchor with no canonical data at all → wildcard → 0.5 (neutral, no block)
    assert canonical_match_score({}, {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}) == 0.5

    # Both sides empty → 0.5 (insufficient data to confirm a match)
    assert canonical_match_score({}, {}) == 0.5

    # Anchor has matching valve_type but no DN/PN → 1.0 (type confirmed, DN/PN wildcard)
    a = {"valve_type": "截止阀"}
    q = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    assert canonical_match_score(a, q) == 1.0


def test_canonical_score_one_sided_valve_type():
    # Anchor has DN/PN but OCR dropped valve_type: one-sided → 0.5 (wildcard, not confirmed)
    a = {"dn": "DN50", "pn": "PN16"}
    q = {"valve_type": "减压阀", "dn": "DN50", "pn": "PN16"}
    assert canonical_match_score(a, q) == 0.5

    # Quote has DN/PN but no valve_type → same treatment in reverse
    a2 = {"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"}
    q2 = {"dn": "DN25", "pn": "PN16"}
    assert canonical_match_score(a2, q2) == 0.5

    # Both sides missing valve_type but DN/PN match → 1.0 (no vt conflict,
    # "everything present matches" — neither side has conflicting info)
    assert canonical_match_score({"dn": "DN50"}, {"dn": "DN50"}) == 1.0


# ─────────────────────────────────────────────────────────────────────────────
#  8b: valve_type family normalization (P0 deterministic-gate fix)
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.services.canonical import normalize_valve_family, valve_type_compatible


def test_valve_family_normalize():
    # 减压阀族: 组/单体/可调式 variants all collapse to one family
    for t in ("减压阀", "减压阀组", "可调式减压阀", "可调式减压阀组",
              "小阻力可调式减压阀", "小阻力可调式减压阀组",
              "小型可调式减压阀", "小型可调式减压阀组"):
        assert normalize_valve_family(t) == "减压阀族", t
    # Non-members keep their own identity (never merged)
    assert normalize_valve_family("真空破坏器") == "真空破坏器"
    assert normalize_valve_family("流量测试") == "流量测试"
    assert normalize_valve_family("止回阀") == "止回阀"
    assert normalize_valve_family("橡胶瓣止回阀") == "橡胶瓣止回阀"
    assert normalize_valve_family(None) is None


def test_valve_type_compatible():
    # same family compatible
    assert valve_type_compatible("减压阀组", "减压阀") is True
    assert valve_type_compatible("减压阀组", "减压阀组") is True
    assert valve_type_compatible("小阻力可调式减压阀组", "小型可调式减压阀") is True
    # wildcard
    assert valve_type_compatible(None, "球阀") is True
    assert valve_type_compatible("球阀", None) is True
    # incompatible: non-valve / cross-type / true subtype
    assert valve_type_compatible("减压阀组", "真空破坏器") is False
    assert valve_type_compatible("闸阀", "流量测试") is False
    assert valve_type_compatible("球阀", "止回阀") is False
    assert valve_type_compatible("橡胶瓣止回阀", "旋启式止回阀") is False
    assert valve_type_compatible("橡胶瓣止回阀", "止回阀") is False


def test_canonical_score_family_compatible():
    # 减压阀组 ↔ 减压阀 with matching DN/PN → recoverable (0.75), NOT a hard 0.0
    a = {"valve_type": "减压阀组", "dn": "DN20", "pn": "PN16"}
    q = {"valve_type": "减压阀", "dn": "DN20", "pn": "PN16"}
    assert canonical_match_score(a, q) == 0.75
    # exact still 1.0
    a2 = {"valve_type": "减压阀组", "dn": "DN80", "pn": "PN16"}
    assert canonical_match_score(a2, dict(a2)) == 1.0


def test_canonical_score_family_blocks_real_conflicts():
    # 真空破坏器 must NOT be compatible with 减压阀族
    assert canonical_match_score(
        {"valve_type": "减压阀组", "dn": "DN20", "pn": "PN16"},
        {"valve_type": "真空破坏器", "dn": "DN20", "pn": "PN16"}) == 0.0
    # 流量测试接口控制阀门 (→流量测试) must NOT align to 闸阀/球阀
    assert canonical_match_score(
        {"valve_type": "闸阀", "dn": "DN25", "pn": "PN16"},
        {"valve_type": "流量测试", "dn": "DN25", "pn": "PN16"}) == 0.0
    # 止回阀 subtypes stay distinct
    assert canonical_match_score(
        {"valve_type": "橡胶瓣止回阀", "dn": "DN50", "pn": "PN16"},
        {"valve_type": "止回阀", "dn": "DN50", "pn": "PN16"}) == 0.0
    # family-compatible never overrides a DN conflict
    assert canonical_match_score(
        {"valve_type": "减压阀组", "dn": "DN20", "pn": "PN16"},
        {"valve_type": "减压阀", "dn": "DN25", "pn": "PN16"}) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  9-11: pipeline._validate_items
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.intelligence.pipeline import ExtractionPipeline


def test_validate_items_ok():
    items = [{"qty": 10.0, "unit_price": 100.0, "total_price": 1000.0, "validation_warning": ""}]
    ExtractionPipeline._validate_items(items)
    assert items[0]["validation_warning"] == ""


def test_validate_items_bad():
    # 10 × 100 = 1000, but total_price = 900 → >5% diff → flag
    items = [{"qty": 10.0, "unit_price": 100.0, "total_price": 900.0, "validation_warning": ""}]
    ExtractionPipeline._validate_items(items)
    assert items[0]["validation_warning"] != ""
    assert "金额不符" in items[0]["validation_warning"]


def test_validate_items_null():
    # None fields → skip (no warning)
    items = [{"qty": None, "unit_price": 100.0, "total_price": 900.0, "validation_warning": ""}]
    ExtractionPipeline._validate_items(items)
    assert items[0]["validation_warning"] == ""

    items2 = [{"qty": 10.0, "unit_price": None, "total_price": 900.0, "validation_warning": ""}]
    ExtractionPipeline._validate_items(items2)
    assert items2[0]["validation_warning"] == ""


# ─────────────────────────────────────────────────────────────────────────────
#  12-18: page_classifier.classify_page
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.intelligence.page_classifier import PageRole, classify_page


def test_page_classifier_quote():
    html = """<table>
    <tr><th>名称</th><th>规格</th><th>单位</th><th>数量</th><th>含税单价</th></tr>
    <tr><td>截止阀</td><td>DN25 PN16</td><td>个</td><td>10</td><td>280</td></tr>
    <tr><td>闸阀</td><td>DN50 PN10</td><td>个</td><td>5</td><td>650</td></tr>
    <tr><td>止回阀</td><td>DN32 PN16</td><td>个</td><td>8</td><td>320</td></tr>
    </table>"""
    cls = classify_page(html)
    assert cls.primary_role == PageRole.QUOTE_TABLE


def test_page_classifier_not_killed_by_total():
    # A quote page's last rows contain "合计" and "盖章" — must still be QUOTE_TABLE
    html = """<table>
    <tr><th>名称</th><th>规格</th><th>单位</th><th>数量</th><th>单价</th><th>合价</th></tr>
    <tr><td>截止阀</td><td>DN25 PN16</td><td>个</td><td>10</td><td>280</td><td>2800</td></tr>
    <tr><td>球阀</td><td>DN50 PN10</td><td>个</td><td>5</td><td>550</td><td>2750</td></tr>
    <tr><td>合计</td><td></td><td></td><td></td><td></td><td>5550</td></tr>
    </table>
    <p>投标总价：5550元</p>
    <p>（盖章）</p>"""
    cls = classify_page(html)
    assert cls.primary_role == PageRole.QUOTE_TABLE


def test_page_classifier_cover():
    html = """<html><body>
    <h1>投标文件</h1>
    <p>投标单位：上海绵存设备有限公司</p>
    <p>投标总价：¥1,258,000.00</p>
    </body></html>"""
    cls = classify_page(html)
    assert cls.primary_role == PageRole.COVER


def test_page_classifier_no_false_cover():
    # "盖章" alone without 投标总价+投标单位 must NOT be COVER
    html = """<html><body>
    <p>以上报价如有问题请联系我司。</p>
    <p>（盖章）</p>
    <p>日期：2026年5月</p>
    </body></html>"""
    cls = classify_page(html)
    assert cls.primary_role != PageRole.COVER


def test_page_classifier_summary():
    html = """<html><body>
    <h2>报价汇总表</h2>
    <table>
    <tr><th>品类</th><th>金额</th></tr>
    <tr><td>阀门</td><td>1200000</td></tr>
    </table>
    </body></html>"""
    cls = classify_page(html)
    assert cls.primary_role == PageRole.SUMMARY


def test_page_classifier_flags():
    # A quote page that also mentions 投标总价 → QUOTE_TABLE primary, has_doc_total=True
    html = """<table>
    <tr><th>名称</th><th>规格</th><th>单位</th><th>数量</th><th>单价</th></tr>
    <tr><td>截止阀</td><td>DN25 PN16</td><td>个</td><td>10</td><td>280</td></tr>
    <tr><td>闸阀</td><td>DN50 PN10</td><td>个</td><td>5</td><td>650</td></tr>
    <tr><td>投标总价</td><td colspan="4">5550</td></tr>
    </table>"""
    cls = classify_page(html)
    assert cls.primary_role == PageRole.QUOTE_TABLE
    assert cls.has_doc_total is True


def test_page_classifier_fixtures():
    expected = {
        "cover_page.html": PageRole.COVER,
        "quote_table_page.html": PageRole.QUOTE_TABLE,
        "quote_last_page.html": PageRole.QUOTE_TABLE,  # NOT killed by 合计/盖章
        "summary_page.html": PageRole.SUMMARY,
        "certificate_page.html": PageRole.OTHER,
    }
    for fname, expected_role in expected.items():
        html = (FIXTURE_DIR / fname).read_text(encoding="utf-8")
        cls = classify_page(html)
        assert cls.primary_role == expected_role, (
            f"{fname}: expected {expected_role}, got {cls.primary_role}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  19-21: anchor_match.match_anchors canonical hard-filter
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.services.anchor_match import match_anchors, SIM_THRESHOLD
from apps.api.services.tender_list import TenderAnchor


def _make_anchor(name: str, spec: str = "", canonical: dict | None = None) -> TenderAnchor:
    a = TenderAnchor(seq=1, name=name, spec=spec)
    a.canonical = canonical or {}
    return a


class _FakeQuote:
    def __init__(self, id: int = 1, supplier_id: int = 1):
        self.id = id
        self.supplier_id = supplier_id


def _unit_vec(dim: int = 4, idx: int = 0) -> list[float]:
    v = [0.0] * dim
    v[idx] = 1.0
    return v


def test_anchor_match_blocks_valve():
    # Even with cosine=1.0, a valve_type conflict must block the match
    anchors = [_make_anchor("截止阀 DN25", canonical={"valve_type": "截止阀", "dn": "DN25"})]
    quotes = [_FakeQuote()]
    q_texts = ["球阀 DN25"]
    q_dns = [25]
    q_canons = [{"valve_type": "球阀", "dn": "DN25"}]

    # Both anchor and quote map to the same unit vector → cosine = 1.0 without mock
    same_vec = [[1.0, 0.0, 0.0, 0.0]]
    with patch("apps.api.services.anchor_match._embed", return_value=same_vec):
        result = match_anchors(anchors, quotes, q_texts, q_dns,
                               quote_canonicals=q_canons)
    # Canonical hard block (截止阀 vs 球阀) → no match
    assert result == []


def test_anchor_match_blocks_pn():
    # PN conflict → hard block even with cosine=1.0
    anchors = [_make_anchor("截止阀 DN25 PN16", canonical={"valve_type": "截止阀", "dn": "DN25", "pn": "PN16"})]
    quotes = [_FakeQuote()]
    q_texts = ["截止阀 DN25 PN25"]
    q_dns = [25]
    q_canons = [{"valve_type": "截止阀", "dn": "DN25", "pn": "PN25"}]

    same_vec = [[1.0, 0.0, 0.0, 0.0]]
    with patch("apps.api.services.anchor_match._embed", return_value=same_vec):
        result = match_anchors(anchors, quotes, q_texts, q_dns,
                               quote_canonicals=q_canons)
    assert result == []


def test_anchor_match_compat():
    # quote_canonicals=None → v2.3 behavior: match by cosine + DN only
    anchors = [_make_anchor("截止阀 DN25", canonical={"valve_type": "截止阀", "dn": "DN25"})]
    quotes = [_FakeQuote()]
    q_texts = ["球阀 DN25"]
    q_dns = [25]

    same_vec = [[1.0, 0.0, 0.0, 0.0]]
    with patch("apps.api.services.anchor_match._embed", return_value=same_vec):
        result = match_anchors(anchors, quotes, q_texts, q_dns,
                               quote_canonicals=None)  # No canonical filtering
    # Without canonical filter, cosine=1.0 > threshold → match is found
    assert len(result) == 1
    assert result[0][0] == 0   # quote_idx=0
    assert result[0][1] == 0   # anchor_idx=0


# ─────────────────────────────────────────────────────────────────────────────
#  22-24: quote_readiness.assess_readiness
# ─────────────────────────────────────────────────────────────────────────────

from apps.api.services.quote_readiness import assess_readiness


def test_readiness_auto_ready():
    # 82 total, 82 valid (0 validation failures), 81 matched, 0 pending, 1 residue
    stats = {
        "quote_rows": 82,
        "matched_rows": 81,
        "pending_rows": 0,
        "residue_rows": 1,
        "aggregated_rows": 0,
        "validation_failed_rows": 0,
        "computed_total": 1_000_000.0,
        "cross_type_conflicts": 0,
    }
    doc_meta = {"bid_total": 1_000_000.0, "bid_total_basis": "tax_included", "tax_rate": 0.13}
    r = assess_readiness(1, "供应商A", stats, doc_meta)
    assert r.auto_matrix_ready is True
    assert r.has_exclusions is True   # 1 residue row → excluded
    assert r.excluded_rows["residue"] == 1
    assert r.checksum_status == "passed"


def test_readiness_checksum_fail():
    # computed_total ≠ doc_total by >5% → checksum failed → auto_matrix_ready=False
    stats = {
        "quote_rows": 10,
        "matched_rows": 10,
        "pending_rows": 0,
        "residue_rows": 0,
        "aggregated_rows": 0,
        "validation_failed_rows": 0,
        "computed_total": 100_000.0,
        "cross_type_conflicts": 0,
    }
    doc_meta = {"bid_total": 200_000.0, "bid_total_basis": "tax_included", "tax_rate": 0.13}
    r = assess_readiness(2, "供应商B", stats, doc_meta)
    assert r.checksum_status == "failed"
    assert r.auto_matrix_ready is False
    assert len(r.reasons) > 0


def test_readiness_pending_excluded():
    # auto_matrix_ready=True but 1 pending row → has_exclusions=True, excluded_rows.pending=1
    stats = {
        "quote_rows": 10,
        "matched_rows": 9,
        "pending_rows": 1,
        "residue_rows": 0,
        "aggregated_rows": 0,
        "validation_failed_rows": 0,
        "computed_total": None,
        "cross_type_conflicts": 0,
    }
    r = assess_readiness(3, "供应商C", stats, doc_meta=None)
    assert r.auto_matrix_ready is True
    assert r.has_exclusions is True
    assert r.excluded_rows["pending"] == 1
    assert r.checksum_status == "unknown"
    assert any("待确认" in w for w in r.warnings)


def test_readiness_checksum_shares_the_ingest_gate_threshold():
    """下游准入门不得比上游入库门宽松（评审 B2）。

    此前 readiness 独立写着 5%、入库门是 0.5%，相差 10 倍。后果不是"多放行
    一点"：偏差 2% 的报价入库时会被拒、需人工 checksum_ack，而 ack 的语义是
    "允许存储"；到了 readiness 却因 2% ≤ 5% 判 passed，又被自动放进比价矩阵，
    等于**下游默默推翻了上游要求的人工判断**。
    """
    from apps.api.core.domain_config import CHECKSUM_BLOCK_DELTA_RATIO
    from apps.api.services.quote_readiness import _CHECKSUM_TOLERANCE, _compute_checksum

    assert _CHECKSUM_TOLERANCE == CHECKSUM_BLOCK_DELTA_RATIO, "两道门必须共用同一个阈值"

    # 2% 偏差：入库门会拒（需 ack），准入门也必须拒
    assert _compute_checksum(1_000_000.0, 980_000.0, "tax_included") == "failed"
    # 阈值内仍然通过
    assert _compute_checksum(1_000_000.0, 997_000.0, "tax_included") == "passed"
    # 税基不可比时既不是 passed 也不是 failed —— 保留这个第三态
    assert _compute_checksum(1_000_000.0, 940_000.0, "tax_excluded") == "basis_mismatch"
