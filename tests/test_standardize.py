"""Tests for material name standardization service."""

import pytest
from apps.api.services.standardize import standardize_name


class TestDNNormalization:
    def test_phi_to_dn(self):
        result = standardize_name("Φ108 热浸镀锌钢管")
        assert "DN100" in result["standardized"]
        assert any("Φ108" in c for c in result["changes"])

    def test_chinese_inch_to_dn(self):
        result = standardize_name("4寸镀锌钢管")
        assert "DN100" in result["standardized"]
        assert any("4寸" in c for c in result["changes"])

    def test_mm_to_dn(self):
        result = standardize_name("100mm 无缝钢管")
        assert "DN100" in result["standardized"]

    def test_existing_dn_untouched(self):
        result = standardize_name("DN100 热浸镀锌钢管")
        assert result["standardized"] == "DN100 热浸镀锌钢管"
        assert not any("DN" in c for c in result["changes"])


class TestDimensionNormalization:
    def test_asterisk_to_times(self):
        result = standardize_name("桥架 300*150")
        assert "300×150" in result["standardized"]

    def test_x_to_times(self):
        result = standardize_name("桥架 300x150")
        assert "300×150" in result["standardized"]


class TestSynonymNormalization:
    def test_hot_dip_galvanize(self):
        result = standardize_name("热镀锌桥架")
        assert "热浸镀锌" in result["standardized"]

    def test_butterfly_valve(self):
        result = standardize_name("蝶型阀 DN100")
        assert "蝶阀" in result["standardized"]

    def test_check_valve(self):
        result = standardize_name("逆止阀")
        assert "止回阀" in result["standardized"]

    def test_fcu_abbreviation(self):
        result = standardize_name("风盘 FP-68")
        assert "风机盘管" in result["standardized"]


class TestWhitespace:
    def test_multiple_spaces(self):
        result = standardize_name("桥架  300*150   热镀锌")
        assert "  " not in result["standardized"]

    def test_fullwidth_space(self):
        result = standardize_name("桥架　300*150")
        assert "　" not in result["standardized"]


class TestEdgeCases:
    def test_empty_string(self):
        result = standardize_name("")
        assert result["standardized"] == ""
        assert result["changes"] == []

    def test_none_input(self):
        result = standardize_name(None)
        assert result["changes"] == []

    def test_no_changes(self):
        result = standardize_name("DN100 托盘式桥架")
        assert result["original"] == result["standardized"]
        assert result["changes"] == []

    def test_combined_normalizations(self):
        result = standardize_name("热镀锌  桥架 300*150 Φ108")
        s = result["standardized"]
        assert "热浸镀锌" in s
        assert "300×150" in s
        assert "DN100" in s
        assert "  " not in s
        assert len(result["changes"]) >= 3
