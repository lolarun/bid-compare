"""design/32 A1：清单里「这一行是不是一条物料」的共用判据。

判据的价值全在两个方向上都不出错：合计行必须拦住，**真条目一条都不能删**。
只测前者等于给"用删行让门通过"发了通行证。
"""
from __future__ import annotations

import pytest

from apps.api.services.ingestion.list_rows import (
    FOOTER_MARKERS, classify_quote_row, text_hits_footer_marker,
)


class TestRowsThatMustBeKept:
    """删掉一条真报价，比放过一条合计行严重得多。"""

    def test_row_with_quantity_is_always_an_item(self):
        """有数量就是条目——名字再像表尾也不改判。"""
        assert classify_quote_row(0, name="合计", spec="DN100", unit="个", qty=3.0) is None

    def test_real_item_with_missing_quantity_is_kept(self):
        """实测形状：识别串列，材质 EPDM 落进 unit、数量丢失，但这是一条
        3,460 元的真实报价（凯硕新正 PDF 第 89 行）。按"无数量即丢弃"处理
        就是静默删钱。"""
        assert classify_quote_row(
            88, name="缓闭式止回阀", spec="DN100", unit="EPDM", qty=None) is None

    def test_ordinary_row_with_empty_unit_is_kept(self):
        assert classify_quote_row(0, name="截止阀", spec="DN25", unit="", qty=None) is None

    def test_blank_row_is_not_claimed_as_aggregate(self):
        """全空行不该被说成"合计行"——它是另一种情况，由调用方的空名判据处理。
        三列同值那条如果不排除空字符串，空行会被误判成表尾标签。"""
        assert classify_quote_row(0, name="", spec="", unit="", qty=None) is None


class TestRowsThatMustBeExcluded:
    def test_label_bleeding_across_all_text_columns(self):
        """实测形状：合计行的标签被识别成名称/规格/单位三列同值。
        这条判据不依赖任何词表，语言无关。"""
        r = classify_quote_row(
            89, name="含税合价（元）：", spec="含税合价（元）：",
            unit="含税合价（元）：", qty=None)
        assert r is not None
        assert r.index == 89
        assert "三列同为" in r.reason

    def test_footer_marker_in_name(self):
        r = classify_quote_row(50, name="合计", spec="", unit="", qty=None)
        assert r is not None and "表尾词" in r.reason

    @pytest.mark.parametrize("label", ["价税合计", "含税合价（元）：", "小计", "总计", "税金"])
    def test_common_footer_labels(self, label):
        assert classify_quote_row(1, name=label, spec="", unit="", qty=None) is not None

    def test_reason_is_showable_to_a_user(self):
        """排除必须能解释给人听——"我们少算了一行"没有说明就是静默删行。"""
        r = classify_quote_row(89, name="含税合价（元）：", spec="含税合价（元）：",
                               unit="含税合价（元）：", qty=None)
        assert r.label in r.reason and len(r.reason) > 10


class TestSharedVocabulary:
    def test_the_marker_that_the_old_quote_side_regex_missed(self):
        """报价侧旧正则有「含税合计」没有「含税合价」，一个字之差漏掉了真实
        文件里的合计行；招标侧词表里本来就有「合价」。统一之后必须覆盖。"""
        assert text_hits_footer_marker("含税合价（元）：")

    def test_vocabulary_covers_the_tender_side_markers(self):
        """招标侧 tender_list._FOOTER_MARKERS 沿用多轮，统一后不能丢词。"""
        for m in ("含税", "合价", "合计", "总计", "小计", "说明", "备注："):
            assert m in FOOTER_MARKERS, f"统一词表丢了招标侧的「{m}」"

    def test_ordinary_material_names_do_not_hit(self):
        for name in ("截止阀", "缓闭式止回阀", "Y型过滤器", "电缆", "蝶阀"):
            assert not text_hits_footer_marker(name), name
