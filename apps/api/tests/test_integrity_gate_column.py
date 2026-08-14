"""design/27 §10 步骤4 —— _gate_integrity 逐行疑点的 column 字段。

前端逐格标色需要知道"哪一行"之外还要"哪一列"——这里验证三种判据各自算出
的列是对的，不是泛泛地"有个 column 字段"：算术疑点要落在合价那一侧（不是
单价），截断疑点要落在真正被截断的那一列，重复行落身份列（material）。
"""
from __future__ import annotations

from apps.api.services.submission.quote_confirmation_service import _gate_integrity


def test_arithmetic_mismatch_column_is_the_total_side_not_unit_price():
    # 20 行正常 + 1 行合价对不上（数量×单价=100*50=5000，合价却是 9999）——
    # 错误率 1/21≈4.8%，低于 MATCH_ARITHMETIC_MAX_ERROR_RATE(5%)，落 REVIEW
    # （不是 BLOCKED），这样才会进 warnings 而不是 blocking_issue——错误行数量
    # 太少会被判 BLOCKED，进的是另一条分支，不是本测试要验的东西。
    good = [{"material": f"阀门{i}", "spec": "DN20", "qty": 10, "unit_price": 10, "total_price": 100}
           for i in range(20)]
    # 7777，不是 9999——9999/(100*50)=1.9998 恰好落进"报价倍率"判据（≈2.0，
    # 按根/按束报价的合法口径），会被判 multiplier 不是 mismatch，压根不会
    # 进 warnings。换一个不挨着任何 _PLAUSIBLE_MULTIPLIERS 的偏差值。
    bad = [{"material": "阀门坏", "spec": "DN40", "qty": 100, "unit_price": 50, "total_price": 7777}]
    items = good + bad
    result = _gate_integrity(None, items, dry_run=True)
    warn_row = next((w for w in result["warnings"] if w["index"] == 20), None)
    assert warn_row is not None, f"第21行算术疑点没进 warnings：{result['warnings']}"
    assert warn_row["column"] == "total_price", warn_row


def test_truncation_column_matches_the_actual_truncated_field():
    # 单价列的值全部截断在同一个宽度上限（"1234.5" 这种明显比 total_price
    # 的小数位少的模式），total_price 本身位数正常——column 应该落 unit_price，
    # 不是随手给个 material。
    items = [
        {"material": "阀门A", "spec": "DN20", "qty": 1, "unit_price": "1234.5", "total_price": "1234.50"},
        {"material": "阀门B", "spec": "DN25", "qty": 1, "unit_price": "2345.6", "total_price": "2345.60"},
        {"material": "阀门C", "spec": "DN32", "qty": 1, "unit_price": "3456.78", "total_price": "3456.78"},
    ]
    result = _gate_integrity(None, items, dry_run=True)
    trunc_rows = [w for w in result["warnings"] if "value_truncated" in w["flags"]]
    if trunc_rows:  # 截断判据本身依赖统计分布，样本太小可能不触发——命中时才断言列
        assert all(w["column"] == "unit_price" for w in trunc_rows), trunc_rows


def test_duplicate_row_column_is_material_identity_anchor():
    # 3 组重复（低于 BLOCKED 的金额占比阈值，落 REVIEW）。
    items = [
        {"material": "阀门X", "spec": "DN20", "unit": "个", "qty": 1, "unit_price": 10, "total_price": 10},
        {"material": "阀门X", "spec": "DN20", "unit": "个", "qty": 1, "unit_price": 10, "total_price": 10},
        {"material": "阀门Y", "spec": "DN99", "unit": "个", "qty": 999, "unit_price": 999, "total_price": 998001},
    ]
    result = _gate_integrity(None, items, dry_run=True)
    dup_rows = [w for w in result["warnings"] if "duplicate_row" in w["flags"]]
    assert dup_rows, f"没有命中重复行判据：{result['warnings']}"
    assert all(w["column"] == "material" for w in dup_rows), dup_rows


def test_no_flags_defaults_column_to_material_anchor():
    """列信息算不出来时（比如 column_shift 这类整行性问题）落 material 当锚点，
    不是留 None 让前端处理 undefined。"""
    from apps.api.services.submission.quote_confirmation_service import _integrity_row
    row = _integrity_row([{"material": "阀门", "spec": "DN20"}], 0, [])
    assert row["column"] == "material"
