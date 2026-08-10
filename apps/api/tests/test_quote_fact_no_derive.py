"""QuoteFact 不得静默派生合价。

背景：`__post_init__` 原本直接 `total_price = unit_price * qty`。派生发生在构造函数里，
**任何**创建 QuoteFact 的路径都会中招，写进去之后事后无法分辨这个数是读来的还是算的；
下游算术校验 |qty×price − total| 因此恒为 0，把列错位、漏读单元格这类真实缺陷全部洗白。
"""
from __future__ import annotations

from apps.api.intelligence.quote_fact import QuoteFact
from apps.api.services.draft_integrity import check_row_arithmetic


def test_missing_total_stays_none_with_candidate():
    """原文无合价 → 权威值保持 None，派生值只进候选。"""
    f = QuoteFact(material="矿物电缆", qty=10.0, unit_price=5.0)
    assert f.total_price is None, "权威合价不得由系统计算"
    assert f.total_source == "missing"
    assert f.derived_total_candidate == 50.0, "候选值要留给人工参考"


def test_read_total_is_marked_ocr():
    f = QuoteFact(material="矿物电缆", qty=10.0, unit_price=5.0, total_price=50.0)
    assert f.total_price == 50.0
    assert f.total_source == "ocr"
    assert f.derived_total_candidate is None


def test_no_candidate_when_inputs_missing():
    f = QuoteFact(material="矿物电缆")
    assert f.total_price is None and f.total_source == "missing"
    assert f.derived_total_candidate is None


def test_source_and_candidate_travel_with_the_row():
    """来源标记必须随行走：否则下游只知道"没有"，不知道是原文没有还是读丢了。"""
    d = QuoteFact(material="矿物电缆", qty=2.0, unit_price=3.0).to_item_dict()
    assert d["total_price"] is None
    assert d["total_source"] == "missing"
    assert d["derived_total_candidate"] == 6.0


def test_derived_row_is_not_evaluable_by_arithmetic_gate():
    """这是这次修复的核心收益：派生行不再让算术校验恒成立。

    旧行为下 total_price 被写成 qty×price，|qty×price − total| 恒为 0，
    真实错误被稀释到阈值以下。现在这类行记为 not_evaluable，不计为通过。
    """
    d = QuoteFact(material="矿物电缆", qty=10.0, unit_price=5.0).to_item_dict()
    assert check_row_arithmetic(d).status == "not_evaluable"


def test_zero_qty_or_price_does_not_fabricate_zero_total():
    """数量或单价为 0 时也不得造出 0 合价——0 是一个会被当真的数字。"""
    for kw in ({"qty": 0.0, "unit_price": 5.0}, {"qty": 10.0, "unit_price": 0.0}):
        f = QuoteFact(material="X", **kw)
        assert f.total_price is None
        assert f.total_source == "missing"
