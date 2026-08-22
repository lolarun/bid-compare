"""design/31 §5 的验收断言。

重点不在"数字算得对不对"（那是几行乘减法），而在**系统有没有把不知道的
事说成知道**——§5.1 撤回的正是那个断言。所以下面一半的用例在检查
`unbounded` 有没有被静默降级成 0、措辞里有没有冒出确定性结论。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api.services.matrix.preview_ordering import (
    PreviewCell,
    PreviewRow,
    build_ordering,
    describe,
)


def _row(anchor: str, qty, **prices) -> PreviewRow:
    """价格为 None 的格子 = 待确认（`confirmable=True`）。

    2026-08-22 起 `PreviewCell` 把"没有可用价"和"待人工确认"拆成了两个字段
    （见该类文档：missing 同样没价，但它不是待办）。这个构造器服务的是
    "待确认"那一档，所以显式置 confirmable；要构造 missing 那一档，用
    `_unpriced_but_not_confirmable`。
    """
    return PreviewRow(anchor, qty, tuple(
        PreviewCell(k, v, confirmable=v is None) for k, v in prices.items()))


def _unpriced_but_not_confirmable(supplier: str) -> PreviewCell:
    """没有价、但**不是**待办的格子——供应商压根没报这一行（missing）。"""
    return PreviewCell(supplier, None, confirmable=False)


# ─── 影响估算的三档 ──────────────────────────────────────────────────────────

def test_two_peers_give_an_estimated_swing():
    rows = [_row("A1", 10, s1=None, s2=100.0, s3=130.0)]
    o = build_ordering(rows)
    assert o.pending_count == 1
    hit = o.queue[0]
    assert hit.kind == "estimated"
    assert hit.swing == pytest.approx(10 * (130 - 100))
    assert hit.peer_count == 2


def test_single_peer_is_unbounded_not_zero_swing():
    """只有一家同行 = 只有一个点，区间宽度 0 **不等于**影响为 0。
    把"无知"写成"确定影响很小"是这个模块最危险的失败模式。"""
    rows = [_row("A1", 10, s1=None, s2=100.0)]
    hit = build_ordering(rows).queue[0]
    assert hit.kind == "unbounded"
    assert hit.swing is None
    # 体量仍然知道，用于组内排序
    assert hit.magnitude == pytest.approx(1000.0)


def test_no_peer_or_no_qty_is_unbounded_without_magnitude():
    assert build_ordering([_row("A1", 10, s1=None)]).queue[0].magnitude is None
    assert build_ordering([_row("A1", None, s1=None, s2=1.0, s3=2.0)]).queue[0].kind == "unbounded"


def test_unbounded_never_contributes_to_the_estimated_total():
    rows = [
        _row("A1", 10, s1=None, s2=100.0),            # unbounded
        _row("A2", 2, s1=None, s2=50.0, s3=60.0),     # estimated 20
    ]
    o = build_ordering(rows)
    assert o.estimated_total_swing == pytest.approx(20.0)
    assert o.unbounded_count == 1


# ─── 排序 ────────────────────────────────────────────────────────────────────

def test_unbounded_rows_come_before_any_estimated_row():
    """哪怕估算值很大也排在后面——读不出来的行系统最没把握，最该先看。"""
    rows = [
        _row("big", 1000, s1=None, s2=100.0, s3=200.0),   # estimated 100000
        _row("blind", 1, s1=None),                        # unbounded, 体量未知
    ]
    q = build_ordering(rows).queue
    assert [i.anchor_key for i in q] == ["blind", "big"]


def test_within_unbounded_unknown_magnitude_first_then_by_size():
    rows = [
        _row("small", 1, s1=None, s2=10.0),      # unbounded, magnitude 10
        _row("blind", 5, s1=None),               # unbounded, magnitude None
        _row("large", 100, s1=None, s2=10.0),    # unbounded, magnitude 1000
    ]
    assert [i.anchor_key for i in build_ordering(rows).queue] == ["blind", "large", "small"]


def test_estimated_rows_sort_by_swing_descending():
    rows = [
        _row("a", 1, s1=None, s2=1.0, s3=2.0),     # swing 1
        _row("b", 1, s1=None, s2=1.0, s3=100.0),   # swing 99
    ]
    assert [i.anchor_key for i in build_ordering(rows).queue] == ["b", "a"]


def test_resolved_cells_never_enter_the_queue():
    rows = [_row("A1", 10, s1=5.0, s2=6.0, s3=7.0)]
    assert build_ordering(rows).pending_count == 0


# ─── 措辞：§5.1 撤回的那个断言不许回来 ───────────────────────────────────────

def test_never_claims_certainty_while_unbounded_rows_exist():
    rows = [_row("blind", 10, s1=None)]
    text = describe(build_ordering(rows), leader_gap=1_000_000.0)
    assert "无法估算" in text
    assert "不对名次是否会变化下结论" in text
    assert "已经确定" not in text and "不会改变" not in text


def test_when_fully_estimable_the_wording_stays_an_estimate():
    rows = [_row("a", 1, s1=None, s2=1.0, s3=2.0)]     # swing 1
    text = describe(build_ordering(rows), leader_gap=1000.0)
    assert "按估算" in text
    assert "非保证" in text
    assert "已经确定" not in text


def test_no_leader_gap_means_no_ranking_statement_at_all():
    rows = [_row("a", 1, s1=None, s2=1.0, s3=2.0)]
    text = describe(build_ordering(rows), leader_gap=None)
    assert "名次" not in text


def test_empty_queue_says_so():
    assert describe(build_ordering([]), leader_gap=None) == "没有待确认项。"


# ─── 真实语料：金桥三家报价清单，序号 1:1 对齐 ──────────────────────────────

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "documents"
_QUOTES = {
    "泰科龙": "金桥地体上盖项目-泰科龙报价清单.xlsx",
    "凯硕新正": "金桥地体上盖项目-凯硕新正报价清单.xlsx",
    "上海绵存": "金桥地体上盖项目-上海绵存报价清单.xlsx",
}


@pytest.mark.skipif(not all((FIXTURES / f).exists() for f in _QUOTES.values()),
                    reason="金桥报价清单夹具缺失")
def test_real_corpus_orders_the_rows_nobody_priced_first():
    """三家真实报价清单（同一份招标，序号 1..89 一一对应）跑一遍：
    队列必须非空、`unbounded` 必须排在最前，且估算合计只由 estimated 贡献。

    这里不断言具体金额——那会把一次识别结果当成契约（`.claude/rules/tests.md`
    的评测/回放不得互相冒充）；断言的是排序与分类这两条不随语料漂移的性质。
    """
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular

    by_supplier: dict[str, list[dict]] = {
        name: extract_quote_tabular(str(FIXTURES / f), {})["items"]
        for name, f in _QUOTES.items()
    }
    n = min(len(v) for v in by_supplier.values())
    assert n > 0

    rows: list[PreviewRow] = []
    for i in range(n):
        first = next(iter(by_supplier.values()))[i]
        rows.append(PreviewRow(
            anchor_key=str(i + 1),
            qty=first.get("qty"),
            cells=tuple(
                PreviewCell(name, items[i].get("unit_price"),
                            confirmable=items[i].get("unit_price") is None)
                for name, items in by_supplier.items()
            ),
        ))

    o = build_ordering(rows)
    # 泰科龙的 xlsx 只有 单价(不含税)，通用 unit_price 槽位为空（design/30
    # §2.3 实测），所以这批语料一定有待确认格子——正好是这个功能要处理的情形。
    assert o.pending_count > 0
    kinds = [i.kind for i in o.queue]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "unbounded" else 1)
    assert o.estimated_total_swing >= 0
    assert o.unbounded_count + sum(1 for k in kinds if k == "estimated") == o.pending_count


# ─── 待确认 ≠ 没有价（2026-08-22 实测缺陷）─────────────────────────────────

def test_missing_cells_do_not_enter_the_queue():
    """供应商没报这一行 = 没有价，但**不是**待办。

    实测：初版用"没有价"当待办判据，同一份数据里 quoted 169 / missing 50 /
    aggregated 36 / pending 9，队列列出 95 条，只有 9 条人能动。用户看到的是
    一屏自己无能为力的条目。
    """
    row = PreviewRow("1", 10.0, (
        PreviewCell("甲", 100.0),
        PreviewCell("乙", 120.0),
        _unpriced_but_not_confirmable("丙"),      # 丙没报价
    ))
    o = build_ordering([row])
    assert o.pending_count == 0, f"missing 被当成待确认了：{o.queue}"


def test_a_pending_cell_still_enters_even_when_a_missing_cell_sits_next_to_it():
    row = PreviewRow("1", 10.0, (
        PreviewCell("甲", 100.0),
        PreviewCell("乙", None, confirmable=True),   # 真待确认
        _unpriced_but_not_confirmable("丙"),
    ))
    o = build_ordering([row])
    assert [i.supplier_key for i in o.queue] == ["乙"]


def test_a_missing_cell_is_not_counted_as_a_peer():
    """没报价的同行不该被当成"有一个同行"——它提供不了任何价格信息。
    只有甲一家有价 → 乙的影响只能是 unbounded，不能算出区间。"""
    row = PreviewRow("1", 10.0, (
        PreviewCell("甲", 100.0),
        PreviewCell("乙", None, confirmable=True),
        _unpriced_but_not_confirmable("丙"),
    ))
    impact = build_ordering([row]).queue[0]
    assert impact.kind == "unbounded"
    assert impact.peer_count == 1


def test_confirmable_defaults_to_false():
    """漏传时宁可漏报一条待办，也不要凭空多报一屏——前者好发现好补。"""
    assert PreviewCell("甲", None).confirmable is False
