"""顺序直连门禁回归测试（anchor_match._sequential_matches）。

锁定防串位行为（CLAUDE.md §8 对齐）：
- 整表按位置 1:1 对齐（行数==锚点 + 序号连续 + DN覆盖≥90% + DN一致≥95% + vt≥70%）；
- 中间漏行/行数不符 → 拒绝（走语义）；
- 相邻异DN交换 → DN一致率掉到阈值下 → 拒绝；
- 同DN异阀型整体交换 → vt一致率掉到阈值下 → 拒绝；
- DN覆盖不足 → 拒绝；
- 单行字段冲突（DN/单位/数量）→ 该行 pending，其余仍 align（不连累整表）。
"""
from __future__ import annotations

from types import SimpleNamespace

from apps.api.services.alignment.anchor_match import _sequential_matches
from apps.api.services.ingestion.canonical import extract_valve_canonical


def _anchor(seq, name, dn, unit="个", qty=1.0):
    return SimpleNamespace(seq=seq, name=name, spec=f"DN{dn}", unit=unit, qty=qty)


def _quote(sub_id, dn, name="蝶阀", unit="个", qty=1.0, dri=None):
    canon = extract_valve_canonical(name, f"DN{dn}")
    q = SimpleNamespace(supplier_id=sub_id, quantity=qty, canonical=canon,
                        document_row_index=dri)
    # 真实 _BQLMatProxy 带 standard_name；大类族(_coarse_family)从它取名
    m = SimpleNamespace(unit=unit, standard_name=name, spec=f"DN{dn}")
    return q, m, (str(dn) if dn else ""), canon


def _build(anchor_specs, quote_specs, sub_id=1):
    """anchor_specs: [(name,dn)]; quote_specs: [(name,dn,unit,qty)] 或 (name,dn)."""
    anchors = [_anchor(i + 1, n, dn) for i, (n, dn) in enumerate(anchor_specs)]
    quotes, materials, dns, canons = [], [], [], []
    for spec in quote_specs:
        name, dn = spec[0], spec[1]
        unit = spec[2] if len(spec) > 2 else "个"
        qty = spec[3] if len(spec) > 3 else 1.0
        q, m, d, c = _quote(sub_id, dn, name, unit, qty)
        quotes.append(q); materials.append(m); dns.append(d); canons.append(c)
    return anchors, quotes, materials, dns, canons


def test_clean_positional_align():
    """同名不同径四行，顺序一致 → 全部直连 align，无冲突，不走 embedding。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100)],
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100)],
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert len(seq) == 4 and len(seq_qi) == 4
    assert conflict == set()
    assert embed == []
    # 位置一一对应
    assert sorted((qi, ai) for qi, ai, _ in seq) == [(0, 0), (1, 1), (2, 2), (3, 3)]


def test_name_variant_minority_still_aligns():
    """少数行名称术语差异（招标"止回阀" vs 报价"倒流防止器"同产品），DN/单位/数量一致：
    整表 vt一致率仍 ≥70% → 接受直连；这些行不进逐行冲突 → 全部 align（不判缺报/pending）。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100), ("止回阀", 65)],
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100), ("倒流防止器", 65)],
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert len(seq_qi) == 5 and conflict == set() and embed == []


def test_count_mismatch_rejected():
    """行数 != 锚点数 → 拒绝直连，全部走 embedding。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80)],
        [("蝶阀", 50), ("蝶阀", 65)],
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert seq == [] and seq_qi == set()
    assert sorted(embed) == [0, 1]


def test_adjacent_diff_dn_swap_rejected():
    """相邻异DN交换 → DN一致率掉到阈值下 → 整表拒绝。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100)],
        [("蝶阀", 65), ("蝶阀", 50), ("蝶阀", 80), ("蝶阀", 100)],  # 前两行交换
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    # 2/4=50% < 95% → 拒绝
    assert seq == [] and len(embed) == 4


def test_same_dn_diff_type_swap_rejected():
    """同DN异阀型整体交换 → DN仍100%，但 vt一致率掉到阈值下 → 拒绝（防同DN交换）。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("闸阀", 50), ("蝶阀", 50), ("闸阀", 50)],
        [("闸阀", 50), ("蝶阀", 50), ("闸阀", 50), ("蝶阀", 50)],  # 全部同DN，阀型错位
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert seq == [] and len(embed) == 4


def test_low_dn_coverage_rejected():
    """DN覆盖率 < 90%（多数行无DN）→ 拒绝（防稀疏DN蒙混）。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100)],
        [("蝶阀", 50), ("法兰", 0), ("垫片", 0), ("螺栓", 0)],  # 仅1/4有DN
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert seq == [] and len(embed) == 4


def test_single_row_qty_conflict_pending_not_align():
    """单行数量冲突（OCR误）→ 该行 pending，其余仍 align（不连累整表）。"""
    a, q, m, d, c = _build(
        [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80), ("蝶阀", 100)],
        [("蝶阀", 50, "个", 1.0), ("蝶阀", 65, "个", 1.0), ("蝶阀", 80, "个", 1.0),
         ("蝶阀", 100, "个", 99.0)],  # 末行 qty=99 与锚点 1.0 冲突
    )
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert len(seq_qi) == 4 and embed == []
    assert conflict == {3}   # 仅末行冲突
    # 冲突行 score=0（→ pending），其余 score=1（→ align）
    score_by_qi = {qi: s for qi, _, s in seq}
    assert score_by_qi[3] == 0.0
    assert all(score_by_qi[i] == 1.0 for i in (0, 1, 2))


def test_local_same_dn_two_row_swap_isolated():
    """89 行中局部两行**同 DN 异大类族**交换（蝶阀↔闸阀，DN相同）：
    整表族一致率仍 ≥90% → 接受直连，但被交换的两行 family 冲突 → 单独 pending，
    不连累其余 87 行（防局部同DN串位的关键回归）。"""
    anchor_specs = [("蝶阀", 50 + i) for i in range(89)]   # 89 行各异 DN
    anchor_specs[40] = ("蝶阀", 999)                       # 第41行 蝶阀 DN999
    anchor_specs[41] = ("闸阀", 999)                       # 第42行 闸阀 DN999（同DN异族）
    quote_specs = list(anchor_specs)
    quote_specs[40], quote_specs[41] = anchor_specs[41], anchor_specs[40]  # 交换这两行
    a, q, m, d, c = _build(anchor_specs, quote_specs)
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert embed == [] and len(seq_qi) == 89
    assert conflict == {40, 41}, f"应只隔离交换的两行，实际 {sorted(conflict)}"
    score_by_qi = {qi: s for qi, _, s in seq}
    assert score_by_qi[40] == 0.0 and score_by_qi[41] == 0.0   # 交换行 pending
    assert sum(1 for v in score_by_qi.values() if v == 1.0) == 87


def test_doc_index_broken_rejects_sequential():
    """document_row_index 部分缺失/重复/不连续（业务序号损坏）→ 禁止顺序直连，回退语义。
    不把损坏序号静默替换成数据库插入顺序。"""
    a = [_anchor(1, "蝶阀", 50), _anchor(2, "蝶阀", 65), _anchor(3, "蝶阀", 80)]
    q, mm, dns, canons = [], [], [], []
    for dn, dri in [(50, 1), (65, None), (80, 1)]:   # 1行缺失 + 重复1 → 损坏
        qq, m, d, ca = _quote(1, dn, "蝶阀", "个", 1.0, dri=dri)
        q.append(qq); mm.append(m); dns.append(d); canons.append(ca)
    doc_index = {i: q[i].document_row_index for i in range(len(q))}
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, mm, dns, canons, doc_index=doc_index)
    assert seq == [] and seq_qi == set()
    assert sorted(embed) == [0, 1, 2]            # 全部回退语义


def test_doc_index_fully_absent_uses_load_order():
    """历史数据完全无 document_row_index → 允许用载入(id)顺序兼容（legacy fallback）。"""
    # _build 的 quotes 默认 dri=None（全无 document_row_index）
    a, q, m, d, c = _build([("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80)],
                           [("蝶阀", 50), ("蝶阀", 65), ("蝶阀", 80)])
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c, doc_index=None)
    assert len(seq_qi) == 3 and conflict == set() and embed == []


def test_document_row_index_used_when_present():
    """有 document_row_index 时按它排序（即使载入顺序被打乱也能正确对齐）。"""
    a = [_anchor(1, "蝶阀", 50), _anchor(2, "蝶阀", 65), _anchor(3, "蝶阀", 80)]
    # 故意按打乱顺序构造 quotes，但用 document_row_index 标注真实文档序
    q, mm, dns, canons = [], [], [], []
    for dn, dri in [(80, 3), (50, 1), (65, 2)]:   # 载入顺序乱，dri 正确
        qq, m, d, ca = _quote(1, dn, "蝶阀", "个", 1.0, dri=dri)
        q.append(qq); mm.append(m); dns.append(d); canons.append(ca)
    doc_index = {i: q[i].document_row_index for i in range(len(q))}
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, mm, dns, canons, doc_index=doc_index)
    assert len(seq_qi) == 3 and conflict == set() and embed == []
    # dri=1(qi=1)→anchor0, dri=2(qi=2)→anchor1, dri=3(qi=0)→anchor2
    assert sorted((qi, ai) for qi, ai, _ in seq) == [(0, 2), (1, 0), (2, 1)]


# ─── 判据泛化：无 DN 品类不得被锁死（评审 B1）────────────────────────────────
#
# 原实现整表门禁只认 DN 一种判据，`dn_cov` 对电缆/桥架/母线槽恒为 0 → 顺序直连
# **永远无法启用**。这不是保守，是按品类锁死：七份基准里阀门有 DN、电缆没有。

def _cable(anchor_qtys, quote_qtys, sub_id=1):
    """无 DN 的品类（电缆）：靠数量序列做位置判据。"""
    anchors = [SimpleNamespace(seq=i + 1, name=f"电缆规格{i}", spec=f"YJV-{i}",
                               unit="米", qty=q)
               for i, q in enumerate(anchor_qtys)]
    quotes, materials, dns, canons = [], [], [], []
    for i, q in enumerate(quote_qtys):
        quotes.append(SimpleNamespace(supplier_id=sub_id, quantity=q,
                                      canonical={}, document_row_index=None))
        materials.append(SimpleNamespace(unit="米", standard_name=f"电缆规格{i}",
                                         spec=f"YJV-{i}"))
        dns.append("")          # 电缆无 DN
        canons.append({})
    return anchors, quotes, materials, dns, canons


def test_non_dn_category_can_use_qty_evidence():
    """电缆等无 DN 品类：数量序列有区分度且逐位一致 → 应当允许顺序直连。

    修复前这里必然走 embedding —— dn_cov=0 让门禁无条件拒绝。
    """
    qtys = [120.5, 88.0, 310.75, 45.0, 999.25, 7.5]
    a, q, m, d, c = _cable(qtys, qtys)
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert len(seq_qi) == len(qtys) and embed == []
    assert conflict == set()


def test_non_dn_category_shuffled_qty_rejected():
    """数量序列被打乱 → 一致率掉下阈值 → 仍应拒绝。放开判据不等于放松防串位。"""
    qtys = [120.5, 88.0, 310.75, 45.0, 999.25, 7.5]
    a, q, m, d, c = _cable(qtys, [88.0, 120.5, 45.0, 310.75, 7.5, 999.25])
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert seq == [] and len(embed) == len(qtys)


def test_uniform_qty_is_not_evidence():
    """**数量全相同不构成判据** —— 打乱重排它照样 100% 一致。

    这是为无 DN 品类开数量判据时最容易漏的一条：不做区分度检查，它就成了一条
    几乎无条件放行的通道（实测：既有两个防串位回归测试立刻被放行，因为那些
    夹具的数量全是 1.0）。
    """
    a, q, m, d, c = _cable([1.0] * 6, [1.0] * 6)
    seq, seq_qi, conflict, embed = _sequential_matches(a, q, m, d, c)
    assert seq == [] and len(embed) == 6


def test_chance_agreement_is_row_count_independent():
    """区分度必须与行数无关：'全同'在 4 行和 100 行都该判为无区分度。

    去重比例做不到——4 行全同是 0.25（看着"还行"），100 行全同是 0.01，
    同一种病给出相反读数。
    """
    from apps.api.services.alignment.anchor_match import _chance_agreement
    assert _chance_agreement([1.0] * 4) == 1.0
    assert _chance_agreement([1.0] * 100) == 1.0
    assert _chance_agreement([]) == 1.0, "没有取值就是没有证据，按最差处理"
    assert _chance_agreement([1.0, 2.0, 3.0, 4.0]) == 0.25
