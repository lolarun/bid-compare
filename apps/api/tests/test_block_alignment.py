"""块级对齐 + 块内保序对齐。

夹具用虚构料号，形态复刻实测：段落顺序相反、一个章节被切成多个小块、
多出无数量的段落标题行、块内多/少行。**价格全部不参与对齐**——招标清单没有价格。
"""
from __future__ import annotations

import pytest

from apps.api.services.alignment.block_alignment import (
    DETERMINISTIC,
    LLM_RESOLVED,
    ORDER_FALLBACK,
    Row,
    align_in_order,
    align_quote_to_anchors,
    assign_blocks,
    drop_section_headers,
    qty_similarity,
    split_blocks,
)


def rows(cat, qtys, start=0, unit="米"):
    return [Row(doc_index=start + i, category=cat, spec=f"{cat}-{q}", unit=unit, qty=q)
            for i, q in enumerate(qtys)]


A_QTYS = [10.5, 20.25, 30.75, 40.125]
B_QTYS = [11.5, 22.25, 33.75, 44.125, 55.5]
ANCHORS = rows("矿物电缆", A_QTYS, 0) + rows("普通电缆", B_QTYS, 100)


# ─── 切块 ────────────────────────────────────────────────────────────────────

def test_split_by_change_not_by_group():
    """同一类目出现两段应当是两个块——合并会抹掉顺序，而顺序正是块内对齐的依据。"""
    rs = rows("A", [1, 2], 0) + rows("B", [3], 10) + rows("A", [4, 5], 20)
    assert [b.key for b in split_blocks(rs)] == ["A", "B", "A"]


def test_split_orders_by_doc_index():
    rs = rows("B", [3], 10) + rows("A", [1, 2], 0)
    assert [b.key for b in split_blocks(rs)] == ["A", "B"]


def test_section_headers_are_separated_not_dropped():
    """无数量无规格的行是被误判成明细的段落标题——要分出来，但必须还给调用方。"""
    rs = rows("矿物电缆", [1.5, 2.5], 0)
    rs.append(Row(doc_index=9, category="电缆头", spec="", qty=None))
    body, headers = drop_section_headers(rs)
    assert len(body) == 2 and len(headers) == 1
    assert headers[0].category == "电缆头"


# ─── 块级对应 ────────────────────────────────────────────────────────────────

def test_reversed_section_order_is_recovered():
    """报价把普通电缆印在前、招标清单矿物在前——块级对应要能纠正。"""
    quote = rows("普通电缆", B_QTYS, 0) + rows("矿物电缆", A_QTYS, 100)
    res = align_quote_to_anchors(quote, ANCHORS)
    assert [b.method for b in res.blocks] == [DETERMINISTIC, DETERMINISTIC]
    assert len(res.aligned) == len(ANCHORS)
    assert res.pending == []


def test_one_anchor_section_split_into_several_quote_blocks():
    """实测某份把「预分支电缆头」单独切了好几段；连续块段要能合并成一个章节。"""
    quote = (rows("矿物电缆", A_QTYS[:2], 0) + rows("预分支", A_QTYS[2:3], 10)
             + rows("矿物电缆", A_QTYS[3:], 20) + rows("普通电缆", B_QTYS, 30))
    res = align_quote_to_anchors(quote, ANCHORS)
    assert len(res.aligned) == len(ANCHORS)


def test_price_never_participates_in_alignment():
    """价格是各家自己报的，招标清单没有——不同的价格不得影响对齐结果。"""
    quote = rows("矿物电缆", A_QTYS, 0) + rows("普通电缆", B_QTYS, 100)
    for i, r in enumerate(quote):
        r.payload["unit_price"] = 1000 + i * 37       # 任意价格
    res = align_quote_to_anchors(quote, ANCHORS)
    assert len(res.aligned) == len(ANCHORS)


def test_rows_are_conserved_when_nothing_matches():
    """数量序列完全对不上时也不能丢行——按文档顺序回退并标注。"""
    quote = rows("未知类目", [7.7, 8.8, 9.9], 0)
    res = align_quote_to_anchors(quote, ANCHORS)
    kept = sum(len(b.quote_rows) for b in res.blocks)
    assert kept == 3, "回退路径必须行数守恒"
    assert all(b.method == ORDER_FALLBACK for b in res.blocks)
    assert any("未经确认" in b.note for b in res.blocks)


def test_llm_resolver_used_only_when_deterministic_fails():
    calls = []

    def resolver(q, a):
        calls.append((q, a))
        return {0: 0}

    quote = rows("矿物电缆", A_QTYS, 0) + rows("普通电缆", B_QTYS, 100)
    res = align_quote_to_anchors(quote, ANCHORS, resolver=resolver)
    assert calls == [], "确定性判定成功时不得调用 LLM"
    assert all(b.method == DETERMINISTIC for b in res.blocks)


def test_llm_resolver_decides_block_level_only():
    """真正退化的情况：两块行数相同、数量全是 1（阀门清单常态），
    数量序列完全无法区分，只能靠类目名称的业务含义判断。"""
    q1 = [1.0] * 4
    anchors = rows("给水阀门", q1, 0) + rows("排水阀门", q1, 100)
    quote = rows("排水系统阀门", q1, 0) + rows("给水系统阀门", q1, 100)
    seen = {}

    def resolver(qs, as_):
        seen["q"], seen["a"] = qs, as_
        # 按类目名称的语义配对——这正是确定性判据做不到、LLM 才能做的事
        out = {}
        for s_ in qs:
            for t in as_:
                if t["category"][:1] in s_["category"]:
                    out[s_["index"]] = t["index"]
        return out

    res = align_quote_to_anchors(quote, anchors, resolver=resolver)
    assert seen, "确定性判不了时必须求助 resolver"
    assert any(b.method == LLM_RESOLVED for b in res.blocks)
    assert all("行级仍按文档行序" in b.note for b in res.blocks if b.method == LLM_RESOLVED)


def test_resolver_refusal_falls_back_without_guessing():
    """resolver 拒答（返回空）时按顺序回退并标注，绝不替它猜。"""
    q1 = [1.0] * 3
    anchors = rows("甲", q1, 0) + rows("乙", q1, 100)
    quote = rows("X", q1, 0) + rows("Y", q1, 100)
    res = align_quote_to_anchors(quote, anchors, resolver=lambda q, a: {})
    assert all(b.method == ORDER_FALLBACK for b in res.blocks)
    assert sum(len(b.quote_rows) for b in res.blocks) == 6


def test_resolver_exception_does_not_break_alignment():
    def boom(q, a):
        raise RuntimeError("resolver down")
    q1 = [1.0] * 3
    anchors = rows("甲", q1, 0) + rows("乙", q1, 100)
    res = align_quote_to_anchors(rows("X", q1, 0) + rows("Y", q1, 100), anchors,
                                 resolver=boom)
    assert sum(len(b.quote_rows) for b in res.blocks) == 6


# ─── 块内保序对齐 ────────────────────────────────────────────────────────────

def test_extra_row_does_not_shift_the_rest():
    """多一行只应局部化成 unmatched，不得让后面全部错位。"""
    anchors = rows("A", [1.5, 2.5, 3.5, 4.5], 0)
    quote = rows("A", [1.5, 2.5, 9.9, 3.5, 4.5], 0)
    pairs = align_in_order(quote, anchors)
    assert sum(1 for p in pairs if p.status == "aligned") == 4
    assert sum(1 for p in pairs if "quote_only" in p.conflicts) == 1


def test_missing_row_is_reported_as_anchor_only():
    anchors = rows("A", [1.5, 2.5, 3.5], 0)
    quote = rows("A", [1.5, 3.5], 0)
    pairs = align_in_order(quote, anchors)
    assert sum(1 for p in pairs if "anchor_only" in p.conflicts) == 1


def test_unit_conflict_goes_pending_not_aligned():
    anchors = rows("A", [1.5, 2.5], 0, unit="米")
    quote = rows("A", [1.5, 2.5], 0, unit="套")
    res = align_quote_to_anchors(quote, anchors)
    assert res.aligned == []
    assert len(res.pending) == 2
    assert all("unit" in p.conflicts for p in res.pending)


def test_qty_similarity_is_order_sensitive():
    assert qty_similarity([1, 2, 3], [1, 2, 3]) == 1.0
    assert qty_similarity([1, 2, 3], [3, 2, 1]) < 1.0
    assert qty_similarity([], [1]) == 0.0


def test_result_dict_exposes_blocks_needing_review():
    quote = rows("未知", [7.7, 8.8], 0)
    d = align_quote_to_anchors(quote, ANCHORS).to_dict()
    assert d["needs_review"], "非确定性判定的块必须暴露出来"
    assert d["anchor_rows"] == len(ANCHORS)
