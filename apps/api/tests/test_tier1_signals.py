"""design/28 §3 Tier 1 信号融合——真实识别产物 + 融合数学的确定性单测。

跟 test_document_classify.py 的分工一致：这里既验真实数据（7 份
`apps/api/tests/fixtures/live_*_result.json`，覆盖 4 份真报价 + 3 份真招
标产物），也验融合公式本身在边界值上的行为——后者不依赖真实识别产物，
用合成的 Tier1Signals 直接构造，保证公式改动有回归网兜住。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api.intelligence.tier1_signals import (
    Tier1Signals,
    classify_tier1,
    extract_tier1_signals_from_job_result,
    fuse_tier1_signals,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ── 真实识别产物：7 份全覆盖（4 报价 + 3 招标），全部要求 strong 置信度 ────
# design/28 §3 的这一层是"识别本来就要跑，零额外成本"——如果连真实产物都
# 判不出 strong，说明融合权重/阈值离真实分布太远，不是"差不多就行"。

REAL_FIXTURE_CASES = [
    ("live_kaishuoxinzheng_quote_result.json", "bid"),
    ("live_real_quote_result.json", "bid"),
    ("live_shanghaimiancun_quote_result.json", "bid"),
    ("live_taikelong_quote_result.json", "bid"),
    ("live_jingqiao_tender_result.json", "tender"),
    ("live_real_tender_result.json", "tender"),
    ("live_tender_approval_result.json", "tender"),
]


@pytest.mark.parametrize("fixture_name,expected_verdict", REAL_FIXTURE_CASES)
def test_real_recognition_output_classifies_strong(fixture_name, expected_verdict):
    job_result = _load(fixture_name)
    result = classify_tier1(job_result)
    assert result.verdict == expected_verdict, (
        f"{fixture_name}: 期望 {expected_verdict}，实得 {result.verdict}"
        f"（score={result.score:.2f}，signals={result.signals}）"
    )
    assert result.confidence == "strong", (
        f"{fixture_name}: 真实识别产物应该给出 strong 置信度，实得 {result.confidence}"
        f"（score={result.score:.2f}）——阈值/权重可能离真实分布太远"
    )


def test_quote_shaped_result_has_price_and_supplier_signal():
    job_result = _load("live_kaishuoxinzheng_quote_result.json")
    sig = extract_tier1_signals_from_job_result(job_result)
    assert sig.price_parse_rate == 1.0
    assert sig.supplier_name_present is True
    assert sig.cover_tender_fields_present is False


def test_tender_shaped_result_has_no_price_and_no_supplier_field():
    job_result = _load("live_jingqiao_tender_result.json")
    sig = extract_tier1_signals_from_job_result(job_result)
    assert sig.price_parse_rate == 0.0
    # 招标侧产物压根没有 supplier_name 这个 key——"不适用"，不是"抽了没抽到"，
    # 这条要跟"抽过但是空"（False）严格区分，融合权重不一样。
    assert sig.supplier_name_present is None
    assert sig.cover_tender_fields_present is True


# ── 抽取适配层的边界情况：合成 job_result，不依赖真实识别产物 ──────────────

def test_extract_supplier_absent_key_is_none_not_false():
    """招标侧 job_result 压根没有 supplier_name 这个 key——跟"报价侧抽了但
    是空字符串"必须区分成不同的证据强度（None vs False），这是融合权重设
    计的前提，专门锁一个合成 case，不依赖真实产物凑出这个边界。"""
    sig = extract_tier1_signals_from_job_result(
        {"project_name": "x", "project_code": "", "items": []})
    assert sig.supplier_name_present is None


def test_extract_supplier_blank_string_is_false():
    sig = extract_tier1_signals_from_job_result(
        {"supplier_name": "  ", "items": []})
    assert sig.supplier_name_present is False


def test_extract_doc_meta_supplier_name_takes_precedence():
    """`_doc_meta.supplier_name` 是识别时抽的封面标量，顶层 `supplier_name`
    可能是 batch-confirm 阶段用户输入覆盖过的——两者都在时以 `_doc_meta`
    为准（这是"这次识别到底看没看到供应商名"的证据，不是业务层填的值）。"""
    sig = extract_tier1_signals_from_job_result({
        "supplier_name": "用户手填名",
        "_doc_meta": {"supplier_name": "识别抽到的名"},
        "items": [],
    })
    assert sig.supplier_name_present is True


def test_extract_price_field_zero_counts_as_filled():
    """跟 document_classify._looks_filled 同一条原则：字面 0 算填过，不能
    跟"这个字段压根不存在"混为一谈——投标方可能真的报了"暂定 0 元"这种行，
    那也是"这份产物结构上像报价单"的证据。"""
    sig = extract_tier1_signals_from_job_result({
        "items": [{"material": "a", "unit_price": 0}, {"material": "b", "unit_price": None}],
    })
    assert sig.price_parse_rate == 0.5


def test_extract_zero_rows():
    sig = extract_tier1_signals_from_job_result({"items": []})
    assert sig.row_count == 0
    assert sig.price_parse_rate == 0.0


def test_zero_rows_short_circuits_to_uncertain():
    """零行没有证据可言，融合函数不该硬算出一个看似笃定的分数——空表不能
    因为"招标侧证据 cover_tender_fields_present 恰好是 True"就被判成
    strong tender，那是巧合不是证据。"""
    signals = Tier1Signals(
        price_parse_rate=0.0, supplier_name_present=None,
        cover_tender_fields_present=True, row_count=0,
    )
    result = fuse_tier1_signals(signals)
    assert result.verdict == "uncertain"
    assert result.confidence == "ambiguous"
    assert result.score == 0.0


# ── 融合数学：合成信号，覆盖分档边界 ───────────────────────────────────────

def test_fuse_all_signals_agree_bid_is_strong():
    signals = Tier1Signals(price_parse_rate=1.0, supplier_name_present=True,
                            cover_tender_fields_present=False, row_count=10)
    result = fuse_tier1_signals(signals)
    assert result.verdict == "bid"
    assert result.confidence == "strong"
    assert result.score == pytest.approx(0.75)


def test_fuse_all_signals_agree_tender_is_strong():
    signals = Tier1Signals(price_parse_rate=0.0, supplier_name_present=None,
                            cover_tender_fields_present=True, row_count=10)
    result = fuse_tier1_signals(signals)
    assert result.verdict == "tender"
    assert result.confidence == "strong"
    assert result.score == pytest.approx(-0.75)


def test_fuse_strong_conflict_tempers_to_moderate_not_strong():
    """价格列填满（强投标证据，权重 0.5）同时抽到招标封面标量（强招标证据，
    权重 0.25）——两路结构性证据互相矛盾，权重更高的一路legitimately 占优，
    但融合结果必须被冲突拉低到 moderate，不能因为价格证据够强就无视冲突
    直接给 strong——"strong" 应该只留给证据一致的情况。"""
    signals = Tier1Signals(price_parse_rate=1.0, supplier_name_present=None,
                            cover_tender_fields_present=True, row_count=10)
    result = fuse_tier1_signals(signals)
    assert result.verdict == "bid"
    assert result.confidence == "moderate", (
        f"两路证据冲突时不该给 strong（score={result.score:.2f}）——"
        "strong 应该只留给证据一致的情况"
    )


def test_fuse_balanced_conflicting_signals_land_uncertain():
    """价格列 75% 填充（中等投标证据）同时抽到招标封面标量（强招标证据）——
    加权后两路证据大致抵消，这才是真正的"说不清"，必须是"不确定"，不能让
    任何一路单方面拍板。"""
    signals = Tier1Signals(price_parse_rate=0.75, supplier_name_present=None,
                            cover_tender_fields_present=True, row_count=10)
    result = fuse_tier1_signals(signals)
    assert result.verdict == "uncertain"
    assert result.confidence == "ambiguous"


def test_fuse_moderate_confidence_band():
    """价格列部分填充（弱投标证据），没有其它证据——应该落在 moderate 档，
    不该被抬到 strong（证据本身就不够强，置信度不能虚高）。"""
    signals = Tier1Signals(price_parse_rate=0.65, supplier_name_present=None,
                            cover_tender_fields_present=False, row_count=10)
    result = fuse_tier1_signals(signals)
    assert result.verdict == "bid"
    assert result.confidence == "moderate"


def test_fuse_price_signal_is_linear_and_symmetric():
    from apps.api.intelligence.tier1_signals import _price_signal
    assert _price_signal(0.0) == pytest.approx(-1.0)
    assert _price_signal(0.5) == pytest.approx(0.0)
    assert _price_signal(1.0) == pytest.approx(1.0)


def test_fuse_supplier_present_none_is_neutral_not_evidence():
    """supplier_name_present=None（这条证据本来就不适用）跟 False（抽过但
    没看到）必须是不同的融合权重——None 不该被当成"抽了但没看到"的弱反
    证据，那会让招标产物被双重计分（cover 证据 + 错误的 supplier 反证据）。"""
    with_none = fuse_tier1_signals(Tier1Signals(
        price_parse_rate=0.0, supplier_name_present=None,
        cover_tender_fields_present=True, row_count=10))
    with_false = fuse_tier1_signals(Tier1Signals(
        price_parse_rate=0.0, supplier_name_present=False,
        cover_tender_fields_present=True, row_count=10))
    # False 比 None 多一路（弱）招标证据，融合分数应该更偏招标（更负）
    assert with_false.score < with_none.score
