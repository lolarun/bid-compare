"""品类分类器单测 —— 覆盖专业/品类串线根因场景。"""

import pytest

from apps.api.services.ingestion.category_classify import (
    classify_category, classify_breakdown, ALL_CATEGORIES,
)


@pytest.mark.parametrize("name,spec,expected", [
    # 给排水专业 + 阀门品名 → 必须识别为阀门(不是给排水)
    ("Y型过滤器", "DN50", "阀门"),
    ("冲洗取水阀（设置锁定装置）", "DN25", "阀门"),
    ("减压型倒流防止器", "DN25", "阀门"),       # 无"阀门"二字
    ("倒流防止器", "DN100", "阀门"),            # 无"阀"字
    ("真空破坏器", "DN25", "阀门"),
    ("截止阀", "DN25 PN16", "阀门"),
    ("旋启式止回阀", "DN80", "阀门"),
    ("暗杆闸阀", "DN100", "阀门"),
    ("球阀", "DN20", "阀门"),
    ("可调式减压阀组", "DN50", "阀门"),
    # 电气
    ("镀锌电缆桥架", "200x100", "桥架"),
    ("密集型母线槽", "1000A", "母线槽"),
    ("照明配电箱", "AL1", "配电箱"),
    # 给排水管/箱/泵
    ("薄壁不锈钢管", "DN65", "不锈钢管"),
    ("不锈钢生活水箱", "10m³", "水箱"),
    ("潜水排污泵", "50WQ", "潜水泵"),
    # 暖通(风阀含"阀"但必须归风口风阀，不能进阀门)
    ("防火阀", "630x320", "风口风阀"),
    ("风量调节阀", "500x250", "风口风阀"),
    ("双层百叶送风口", "400x200", "风口风阀"),
    ("风机盘管", "FP-68", "风机盘管"),
    ("空调冷冻水泵", "100m³/h", "空调泵"),
])
def test_classify_known(name, spec, expected):
    g = classify_category(name, spec)
    assert g.category == expected, f"{name} → {g.category} ({g.reason}), expected {expected}"
    assert not g.is_unknown
    assert g.category in ALL_CATEGORIES


@pytest.mark.parametrize("name", [
    "给排水",            # 专业名，不是品类 → unknown
    "电气",
    "暖通工程",
    "施工措施费",
    "",
])
def test_classify_unknown(name):
    g = classify_category(name)
    assert g.is_unknown, f"{name} 应为 unknown, 得到 {g.category}"


def test_profession_not_category():
    """关键回归：专业'给排水'不得被识别成品类。"""
    g = classify_category("给排水")
    assert g.category != "给排水"
    assert g.is_unknown


def test_breakdown_single_category():
    items = [{"name": "Y型过滤器", "spec": "DN50"},
             {"name": "截止阀", "spec": "DN25"},
             {"name": "球阀", "spec": "DN20"}]
    breakdown, detected, has_multiple, unknown = classify_breakdown(items)
    assert detected == "阀门"
    assert has_multiple is False
    assert unknown == 0
    assert breakdown == {"阀门": 3}


def test_breakdown_multi_category():
    items = [{"name": "Y型过滤器", "spec": "DN50"},
             {"name": "截止阀", "spec": "DN25"},
             {"name": "薄壁不锈钢管", "spec": "DN65"},
             {"name": "施工措施费", "spec": ""}]  # unknown
    breakdown, detected, has_multiple, unknown = classify_breakdown(items)
    assert detected == "阀门"          # 多数派
    assert has_multiple is True
    assert unknown == 1
    assert breakdown == {"阀门": 2, "不锈钢管": 1}
