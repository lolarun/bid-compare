"""「原文明确不报价」必须与「读不到合价」分开。

背景：某份投标文件某一项写「/」表示不报此项。系统把它当成缺失合价触发 422，
**逼着用户编一个金额出来**——正好制造了这套系统最该防的东西。
"""
from __future__ import annotations

import pytest

from apps.api.services.draft_integrity import (
    AMOUNT_EMPTY,
    AMOUNT_NOT_QUOTED,
    AMOUNT_VALUE,
    classify_amount_cell,
)
from apps.api.services.quote_confirmation_service import _num_or_none


@pytest.mark.parametrize("raw", ["/", "／", "－", "—", "无", "不报", "不报价",
                                 "N/A", "n/a", "NA", "nil", "None", "×"])
def test_explicit_not_quoted_markers(raw):
    assert classify_amount_cell(raw) == AMOUNT_NOT_QUOTED


@pytest.mark.parametrize("raw", [0, 0.0, 1234, "1234", "1,234.56", " 78.10 ", "¥99"])
def test_numeric_cells(raw):
    assert classify_amount_cell(raw) == AMOUNT_VALUE


@pytest.mark.parametrize("raw", [None, "", "   ", "　"])
def test_empty_is_not_the_same_as_not_quoted(raw):
    """空白是缺陷（该有金额却没读到），不是"不报价"。两者处置完全不同。"""
    assert classify_amount_cell(raw) == AMOUNT_EMPTY


def test_negative_number_is_not_a_marker():
    """「-」独占一格才是"不报"；出现在数字里是负号。"""
    assert classify_amount_cell("-1234.5") == AMOUNT_VALUE
    assert classify_amount_cell("-") == AMOUNT_NOT_QUOTED


def test_unknown_text_is_empty_not_not_quoted():
    """认不出来的文本按"读不到"处理——**不能猜成不报价**，那会静默放行一个缺陷。"""
    assert classify_amount_cell("待定") == AMOUNT_EMPTY
    assert classify_amount_cell("见附页") == AMOUNT_EMPTY


def test_num_or_none_never_raises_on_markers():
    """转不了就 None，不能抛——抛出去会被行级 try 吞掉，整行以"处理失败"被跳过，
    用户看到的是"这行没识别出来"而不是"这行没报价"。"""
    for raw in ("/", "无", "N/A", "待定", "", None, object()):
        assert _num_or_none(raw) is None


def test_num_or_none_still_parses_numbers():
    assert _num_or_none("1234.5") == 1234.5
    assert _num_or_none(78) == 78.0
    assert _num_or_none(0) == 0.0
