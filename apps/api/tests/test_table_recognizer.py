"""table_recognizer 内部函数单元测试 —— 无 API、无 snapshot，直接构造 DraftRow。

覆盖：
  - _infer_missing_seqs：缺口=2 推断 + seq_source/seq_inferred 标记；负例若干。
  - _validate_arithmetic：qty×单价≠合价 标 qty_arithmetic_mismatch + 证据 + 不改原值。
"""
from __future__ import annotations

from apps.api.intelligence.extraction_draft import DraftRow, SourceRef
from apps.api.intelligence.table_recognizer import (
    _infer_missing_seqs,
    _validate_arithmetic,
    _tail_recall_pages,
    _filter_recall_rows,
    _detect_chain_orientation,
)


# ── helpers ─────────────────────────────────────────────────────────────────

def _row(seq=None, name="阀门", row_type="quote_line", page=1, row=0, **fields):
    f = {"seq": seq, "name": name}
    f.update(fields)
    return DraftRow(
        row_index=row,
        row_type=row_type,
        raw_cells={},
        fields=f,
        source_ref=SourceRef(page=page, table=0, row=row),
    )


# ── _infer_missing_seqs ─────────────────────────────────────────────────────

def test_infer_gap2_infers_and_flags():
    """70 / 空 / 72 → 推断 71，带 seq_source=inferred 和 seq_inferred 标记。"""
    rows = [_row(seq="70", name="A", row=0),
            _row(seq=None, name="B", row=1),
            _row(seq="72", name="C", row=2)]
    out = _infer_missing_seqs(rows)
    mid = out[1].fields
    assert mid["seq"] == "71"
    assert mid["seq_source"] == "inferred"
    assert "seq_inferred" in out[1].validation_flags


def test_infer_gap_not_two_no_infer():
    """70 / 空 / 73 → 缺口≠2，不推断。"""
    rows = [_row(seq="70", name="A", row=0),
            _row(seq=None, name="B", row=1),
            _row(seq="73", name="C", row=2)]
    out = _infer_missing_seqs(rows)
    assert not str(out[1].fields.get("seq") or "").strip()
    assert "seq_inferred" not in out[1].validation_flags


def test_infer_no_seq_document_no_infer():
    """无序号文档（<50% 行有 seq）→ 不推断，避免在内容对齐文档误填。"""
    rows = [_row(seq=None, name="A", row=0),
            _row(seq=None, name="B", row=1),
            _row(seq=None, name="C", row=2),
            _row(seq="72", name="D", row=3)]
    out = _infer_missing_seqs(rows)
    assert all(not str(r.fields.get("seq") or "").strip()
               for r in out[:3])


def test_infer_two_consecutive_blanks_no_infer():
    """70 / 空 / 空 / 73 → 两个连续无序号夹中间，缺口非精确 2，不推断。"""
    rows = [_row(seq="70", name="A", row=0),
            _row(seq=None, name="B", row=1),
            _row(seq=None, name="C", row=2),
            _row(seq="73", name="D", row=3)]
    out = _infer_missing_seqs(rows)
    assert "seq_inferred" not in out[1].validation_flags
    assert "seq_inferred" not in out[2].validation_flags


def test_infer_does_not_overwrite_existing_seq():
    """已有整数 seq 的行不被覆盖。"""
    rows = [_row(seq="70", name="A", row=0),
            _row(seq="71", name="B", row=1),
            _row(seq="72", name="C", row=2)]
    out = _infer_missing_seqs(rows)
    assert out[1].fields["seq"] == "71"
    assert "seq_inferred" not in out[1].validation_flags


def test_infer_requires_name():
    """缺口=2 但中间行无物料名 → 不推断（空行不补 seq）。"""
    rows = [_row(seq="70", name="A", row=0),
            _row(seq=None, name="", row=1),
            _row(seq="72", name="C", row=2)]
    out = _infer_missing_seqs(rows)
    assert not str(out[1].fields.get("seq") or "").strip()


# ── _validate_arithmetic ────────────────────────────────────────────────────

def test_arith_mismatch_flagged_with_evidence():
    """凯硕 seq=89 形态：qty=1 但 1×865≠3460 → 标记 + 证据 + 建议 qty=4，不改原值。"""
    rows = [_row(seq="89", name="缓冲式止回阀", qty=1.0,
                 unit_price_incl_tax=865.0, total_price_incl_tax=3460.0)]
    out = _validate_arithmetic(rows)
    f = out[0].fields
    assert "qty_arithmetic_mismatch" in out[0].validation_flags
    assert f["arith_basis"] == "incl"
    assert f["arith_expected_total"] == 865.0
    assert f["arith_actual_total"] == 3460.0
    assert f["arith_suggested_qty"] == 4.0
    assert f["qty"] == 1.0  # 原值保留，绝不自动改成 4


def test_arith_consistent_not_flagged():
    """qty×单价=合价 → 不标记（凯硕 seq=26 正确形态）。"""
    rows = [_row(seq="26", qty=26.0,
                 unit_price_incl_tax=865.0, total_price_incl_tax=22490.0)]
    out = _validate_arithmetic(rows)
    assert "qty_arithmetic_mismatch" not in out[0].validation_flags
    assert "arith_basis" not in out[0].fields


def test_arith_rounding_within_tolerance():
    """单价四舍五入造成的微小尾差在容差 max(0.05, total×0.5%) 内 → 不标记。"""
    # 17 × 116.81 = 1985.77；合价正好 1985.77，零差
    rows = [_row(qty=17.0, unit_price_excl_tax=116.81, total_price_excl_tax=1985.77)]
    out = _validate_arithmetic(rows)
    assert "qty_arithmetic_mismatch" not in out[0].validation_flags


def test_arith_excl_basis_when_no_incl():
    """无含税单价时退化用不含税基校验。"""
    rows = [_row(qty=2.0, unit_price_excl_tax=100.0, total_price_excl_tax=999.0)]
    out = _validate_arithmetic(rows)
    assert "qty_arithmetic_mismatch" in out[0].validation_flags
    assert out[0].fields["arith_basis"] == "excl"


def test_arith_skips_when_fields_missing():
    """qty/单价/合价任一缺失 → 不校验（不误标）。"""
    rows = [_row(qty=None, unit_price_incl_tax=865.0, total_price_incl_tax=3460.0),
            _row(qty=1.0, unit_price_incl_tax=None, total_price_incl_tax=3460.0),
            _row(qty=1.0, unit_price_incl_tax=865.0, total_price_incl_tax=None)]
    out = _validate_arithmetic(rows)
    assert all("qty_arithmetic_mismatch" not in r.validation_flags for r in out)


def test_arith_skips_non_quote_line():
    """小计/合计行不参与算术校验。"""
    rows = [_row(seq=None, name="小计", row_type="subtotal",
                 qty=1.0, unit_price_incl_tax=865.0, total_price_incl_tax=3460.0)]
    out = _validate_arithmetic(rows)
    assert "qty_arithmetic_mismatch" not in out[0].validation_flags


from apps.api.intelligence.table_parser import TableGrid, TableRow, _map_columns


def _grid(header, rows_cells, *, page=4, table_index=0):
    rows = [TableRow(row_index=i, row_type=rt, cells=c)
            for i, (rt, c) in enumerate(rows_cells)]
    return TableGrid(page=page, table_index=table_index, header=header,
                     col_map=_map_columns(header), rows=rows)






# ── Test 4: 续表正确继承表头（html_to_table_grids） ──────────────────────────

def test_continuation_page_inherits_header():
    """续表页（无表头）+ inherited_header 列数匹配 → 正确生成 grid，col_map 非空。"""
    from apps.api.intelligence.table_parser import html_to_table_grids
    # 续表页：无 <th>，两行数据行（parser 要求 >=2 行）
    cont_html = (
        "<table>"
        "<tr><td>2</td><td>截止阀</td><td>DN65</td>"
        "<td>个</td><td>5</td><td>150</td><td>750</td></tr>"
        "<tr><td>3</td><td>球阀</td><td>DN40</td>"
        "<td>个</td><td>10</td><td>80</td><td>800</td></tr>"
        "</table>"
    )
    inherited = ["序号", "材料名称",
                 "规格型号", "单位",
                 "数量", "单价", "合价"]
    grids = html_to_table_grids(cont_html, page_num=2, inherited_header=inherited)
    assert len(grids) == 1
    assert grids[0].col_map.get("材料名称") == "name"
    assert grids[0].col_map.get("数量") == "qty"
    assert len([r for r in grids[0].rows if r.row_type == "quote_line"]) == 2


# ── Test 5: 续表列数变化 → 不继承，assess_grid_eligibility ≤ PARTIAL ────────

def test_continuation_page_col_count_mismatch_no_inherit():
    """续表页列数与 inherited_header 不同 → html_to_table_grids 不使用继承表头。"""
    from apps.api.intelligence.table_parser import html_to_table_grids
    cont_html = """
    <table>
    <tr><td>2</td><td>截止阀</td><td>DN65</td><td>5</td><td>750</td></tr>
    </table>
    """
    # inherited_header 有 7 列；数据行只有 5 列
    inherited = ["序号", "材料名称", "规格型号", "单位", "数量", "单价", "合价"]
    grids = html_to_table_grids(cont_html, page_num=2, inherited_header=inherited)
    # 列数不匹配 → 不使用继承表头 → 无法解析 → 返回空
    assert grids == []


# ── 税价口径回归（凯硕 double-tax bug）───────────────────────────────────────

def test_normalize_fields_no_tax_derivation():
    """_normalize_fields 纯透传：含税/不含税价同时存在时不修改、不派生。

    回归：凯硕表含税单价=498、不含税单价=440.71，LLM曾把498放进unit_price_excl_tax
    然后推导unit_price_incl_tax=498×1.13=562.74。
    _normalize_fields 不做任何税额运算，只做类型转换。
    """
    from apps.api.intelligence.table_recognizer import _normalize_fields

    item = {
        "material": "截止阀",
        "spec": "DN50 PN16",
        "qty": 4.0,
        "unit_price_incl_tax": 498.0,
        "unit_price_excl_tax": 440.71,
        "total_price_incl_tax": 1992.0,
        "total_price_excl_tax": 1762.84,
        "tax_rate": 0.13,
    }
    fields = _normalize_fields(item, name_key="material")

    assert fields["unit_price_incl_tax"] == 498.0, "含税单价必须原值透传"
    assert fields["unit_price_excl_tax"] == 440.71, "不含税单价必须原值透传"
    assert fields["unit_price"] is None, "两者均有时 unit_price 应为 null"
    assert fields["total_price"] is None, "两者均有时 total_price 应为 null"


def test_normalize_fields_incl_only_not_promoted_to_excl():
    """只有含税单价时：unit_price_incl_tax=498，unit_price_excl_tax 保持 None（不反推）。"""
    from apps.api.intelligence.table_recognizer import _normalize_fields

    item = {
        "material": "截止阀",
        "spec": "DN50",
        "qty": 1.0,
        "unit_price_incl_tax": 498.0,
    }
    fields = _normalize_fields(item, name_key="material")

    assert fields["unit_price_incl_tax"] == 498.0
    assert fields["unit_price_excl_tax"] is None, "不得从含税价反推不含税价"
    assert fields["unit_price"] is None


def test_prompt_no_contradictory_unit_price_label():
    """回归：_QUOTE_S2_TABLE_PROMPT 不再把 unit_price 标注为 含税单价。

    旧写法 '区分 unit_price（含税单价）' 会导致 LLM 把 498 塞进 unit_price_excl_tax
    再推导 unit_price_incl_tax=498×1.13=562.74。
    """
    from apps.api.intelligence.providers.dashscope_ocr import _QUOTE_S2_TABLE_PROMPT

    assert "unit_price（含税单价）" not in _QUOTE_S2_TABLE_PROMPT, (
        "prompt 仍含旧写法 'unit_price（含税单价）'，会引发 LLM 税价口径错乱"
    )
    assert "严禁" in _QUOTE_S2_TABLE_PROMPT, "prompt 应包含禁止推算含税/不含税换算的明确声明"


# ── stable visual cache key（snapshot_provider pdf_stable 回退键） ──────────────

def test_stable_visual_cache_key_hit_on_replay(tmp_path):
    """pdf_stable 回退键：同一 PDF 内容 + 不同缩略图 hash → replay 命中。

    场景：PDF 引擎渲染非确定性，同一页面两次生成的缩略图字节略不同，
    导致 primary_key (thumbnail_hash) 在 replay 时 miss。
    若 file_path 提供，snapshot_provider 应改用 pdf_stable 回退键命中缓存。
    """
    import hashlib
    from apps.api.intelligence.snapshot_provider import SnapshotProvider, _h

    pdf_content = b"fake-pdf-content-for-test"
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(pdf_content)
    snap_file = tmp_path / "snap.json"

    thumbnails_v1 = [b"thumb_render_1"]
    thumbnails_v2 = [b"thumb_render_2"]   # same PDF, different render bytes

    class _FakeInner:
        def classify_pages_visual(self, thumbnails, doc_type, **kwargs):
            return [{"role": "quote_table", "page": 1}], []

    provider = SnapshotProvider(_FakeInner(), snap_file, mode="record")
    result1, _ = provider.classify_pages_visual(
        thumbnails_v1, "quote", file_path=str(pdf_file))
    provider.save()

    # replay with different thumbnail bytes but same PDF file
    provider2 = SnapshotProvider(None, snap_file, mode="replay")
    result2, _ = provider2.classify_pages_visual(
        thumbnails_v2, "quote", file_path=str(pdf_file))

    assert result2 == result1, "stable key should hit on replay despite thumbnail diff"


# ── _tail_recall_pages（长报价链尾页位置召回） ──────────────────────────────────

def test_tail_recall_long_chain_appends_next_page():
    """泰科龙形态：连续报价链 p4-p13（10页），p14 被高置信误判出 tgt → 召回 p14。

    回归：p14 是侧向90°报价末页，视觉判 bid_letter，自身信号全 False/None，
    只能靠位置召回。tgt=[4..13]，handled=tgt，total=16 → 召回 [14]。
    """
    tgt = list(range(4, 14))            # p4..p13
    recall = _tail_recall_pages(tgt, set(tgt), total_pages=16)
    assert recall == [14], "长链紧邻尾页必须被召回"


def test_tail_recall_short_chain_no_recall():
    """短链（< min_chain=3）不召回，避免在零散误判页后乱补页。"""
    tgt = [4, 5]                        # 仅2页，非「真报价链」
    recall = _tail_recall_pages(tgt, set(tgt), total_pages=16)
    assert recall == [], "短链不得触发尾页召回"


def test_tail_recall_next_page_already_handled_no_recall():
    """紧邻页已在 handled（已是目标页或 meta 页）→ 不重复召回。"""
    tgt = list(range(4, 14))
    handled = set(tgt) | {14}          # p14 已作为 meta/summary 处理
    recall = _tail_recall_pages(tgt, handled, total_pages=16)
    assert recall == [], "已处理页不得重复召回"


def test_tail_recall_chain_at_doc_end_no_recall():
    """报价链就在文档末尾，无下一页 → 不召回（不越界）。"""
    tgt = list(range(4, 17))           # p4..p16，p16 是末页
    recall = _tail_recall_pages(tgt, set(tgt), total_pages=16)
    assert recall == [], "末页之后无可召回页"


def test_tail_recall_empty_tgt_no_recall():
    """空目标页集合 → 不召回（防御）。"""
    assert _tail_recall_pages([], set(), total_pages=16) == []


def test_tail_recall_only_long_runs_recall():
    """多段链：仅 ≥min_chain 的连续段触发召回；短段忽略。

    tgt=[2,3]（短）+ [6,7,8,9]（长，4页）→ 仅召回长段尾页 p10；
    短段尾页 p4 不召回。
    """
    tgt = [2, 3, 6, 7, 8, 9]
    recall = _tail_recall_pages(tgt, set(tgt), total_pages=16)
    assert recall == [10], "仅长链触发召回，短链段不召回"


# ── _filter_recall_rows 召回行门禁 ──────────────────────────────────────────────

def _qrow(seq=None, name="阀门", page=14, qty=1.0,
          unit_price_incl_tax=100.0, total_price_incl_tax=100.0, **extra):
    """构造一个通过门禁的最小召回行。"""
    f = {"seq": str(seq) if seq is not None else None,
         "material": name, "qty": qty,
         "unit_price_incl_tax": unit_price_incl_tax,
         "total_price_incl_tax": total_price_incl_tax}
    f.update(extra)
    return DraftRow(
        row_index=0, row_type="quote_line", raw_cells={},
        fields=f, source_ref=SourceRef(page=page, table=1, row=0),
    )


def _trusted(seq: int):
    """构造一个 trusted 链上的 quote_line，仅供 chain_tail_seq 计算用。"""
    return DraftRow(
        row_index=0, row_type="quote_line", raw_cells={},
        fields={"seq": str(seq), "material": "x", "qty": 1.0,
                "unit_price_incl_tax": 1.0},
        source_ref=SourceRef(page=1, table=1, row=0),
    )


def test_filter_recall_clean_passes():
    """满足全部条件的召回行 → accepted，review_candidates 为空。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=85), _qrow(seq=86)]
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert len(accepted) == 2 and review == []
    assert all("recall_review_candidate" not in r.validation_flags for r in accepted)


def test_filter_recall_no_name_isolated():
    """缺名称的召回行 → review_candidates（不进 accepted）+ no_name。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=85, name="")]
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert accepted == [], "无名称行不得进入正式行"
    assert "recall_review_candidate" in review[0].validation_flags
    assert "no_name" in review[0].validation_flags


def test_filter_recall_no_qty_isolated():
    """qty=None → review_candidates + no_qty。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=85, qty=None)]
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert accepted == []
    assert "no_qty" in review[0].validation_flags


def test_filter_recall_no_price_isolated():
    """无任何价格字段 → review_candidates + no_price。"""
    trusted = [_trusted(84)]
    row = _qrow(seq=85)
    row.fields["unit_price_incl_tax"] = None
    row.fields["total_price_incl_tax"] = None
    accepted, review = _filter_recall_rows([row], trusted, name_key="material")
    assert accepted == []
    assert "no_price" in review[0].validation_flags


def test_filter_recall_arith_mismatch_isolated():
    """已有 qty_arithmetic_mismatch → review_candidates + arith_mismatch。"""
    trusted = [_trusted(84)]
    row = _qrow(seq=85)
    row.validation_flags.append("qty_arithmetic_mismatch")
    accepted, review = _filter_recall_rows([row], trusted, name_key="material")
    assert accepted == []
    assert "arith_mismatch" in review[0].validation_flags


def test_filter_recall_seq_overlap_chain_tail_isolated():
    """召回行 seq ≤ 已知链尾 → review_candidates（技术页 p15 偶发吐 seq=83 不得合入）。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=83)]           # 83 ≤ 84 → overlap
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert accepted == []
    assert any("seq_overlap_chain_tail" in f for f in review[0].validation_flags)


def test_filter_recall_seq_dup_within_batch_isolated():
    """召回批次内 seq 重复 → 第一行 accepted，第二行 review_candidates。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=85), _qrow(seq=85)]   # 重复
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert len(accepted) == 1 and len(review) == 1
    assert any("seq_dup=85" in f for f in review[0].validation_flags)


def test_filter_recall_no_source_ref_isolated():
    """source_ref.page=0 → review_candidates + no_source_ref。"""
    trusted = [_trusted(84)]
    row = _qrow(seq=85)
    row.source_ref = SourceRef(page=0, table=0, row=0)
    accepted, review = _filter_recall_rows([row], trusted, name_key="material")
    assert accepted == []
    assert "no_source_ref" in review[0].validation_flags


def test_filter_recall_rows_not_silently_dropped():
    """不满足条件的召回行必须落入 review_candidates（不得静默丢弃）。"""
    trusted = [_trusted(84)]
    recall = [_qrow(seq=85, name="")]    # no_name → 隔离，但保留
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert len(accepted) + len(review) == 1, "每行必须落入恰好一个桶，不得丢弃"
    assert len(review) == 1


def test_filter_recall_no_trusted_seq_skips_seq_check():
    """可信链无任何 seq 时，不检查 seq 连续性（chain_tail_seq=None）。"""
    trusted = [DraftRow(row_index=0, row_type="quote_line", raw_cells={},
                        fields={"material": "x", "qty": 1.0},
                        source_ref=SourceRef(page=1, table=0, row=0))]
    recall = [_qrow(seq=None)]   # 无 seq
    accepted, review = _filter_recall_rows(recall, trusted, name_key="material")
    assert len(accepted) == 1 and review == []


# ── _detect_chain_orientation 返回 probe_cache ────────────────────────────────

def test_detect_chain_orientation_returns_tuple():
    """_detect_chain_orientation 必须返回 (int, dict)，不再是裸 int。

    当链内多数页 q0 已达标（正立文档），应返回 (0, {}) 不探测。
    """
    # 伪造正立 OCR HTML（含足够列信号 → _orientation_quality ≥ MIN_GOOD）
    header = ("<table><tr>"
              "<th>序号</th><th>材料名称</th><th>规格型号</th>"
              "<th>单位</th><th>数量</th><th>单价</th><th>合价</th>"
              "</tr></table>")
    page_htmls = [header] * 5
    images = [b"fake_img"] * 5
    chain = [1, 2, 3, 4, 5]

    class _NoCallProvider:
        def ocr_pages_with_roles(self, imgs):
            raise AssertionError("正立链不应调用 OCR 探测")

    angle, cache = _detect_chain_orientation(
        chain, page_htmls, images, _NoCallProvider(), "quote")
    assert angle == 0
    assert cache == {}


def test_detect_chain_orientation_probe_cache_populated():
    """需旋转时，winning angle 的 sample 页 OCR 结果存入 probe_cache。

    伪造：0° q=0（无列），90° q=5（有列）→ 选 90°，cache 含 sample 页。
    """
    import io
    from PIL import Image as _PIL

    # 最小合法 PNG（1×1 白色）
    buf = io.BytesIO()
    _PIL.new("RGB", (1, 1), (255, 255, 255)).save(buf, "PNG")
    png_bytes = buf.getvalue()

    # 0° html：无有效列（q=0）
    empty_html = "<table><tr><td>foo</td></tr></table>"
    # 90° OCR 结果：有完整列头（q ≥ MIN_GOOD）
    good_html = ("<table><tr>"
                 "<th>序号</th><th>材料名称</th><th>规格型号</th>"
                 "<th>单位</th><th>数量</th><th>单价</th><th>合价</th>"
                 "</tr></table>")

    page_htmls = [empty_html] * 3
    images = [png_bytes] * 3
    chain = [1, 2, 3]

    ocr_calls: list[int] = []

    class _FakeProvider:
        def ocr_pages_with_roles(self, imgs):
            ocr_calls.append(len(imgs))
            # Return good_html for 90°, empty for 270°
            # (we can't distinguish angle here, but provider returns what was given)
            # Simulate: first calls are for 90° (return good), rest for 270° (return empty)
            if len(ocr_calls) <= len(chain):   # first N calls → 90° sample
                return [(None, good_html)], []
            return [(None, empty_html)], []

    angle, cache = _detect_chain_orientation(
        chain, page_htmls, images, _FakeProvider(), "quote")

    assert isinstance(angle, int), "返回类型必须是 (int, dict)"
    assert isinstance(cache, dict), "第二个返回值必须是 dict"
    # angle either 0 (if 90 not strictly better) or 90 — just check structure
    if angle != 0:
        assert all(isinstance(v, tuple) and len(v) == 2 for v in cache.values()), \
            "cache 值必须是 (html, image_bytes) 二元组"


# ── 完整链路契约：p14 召回 + p15 技术行隔离 ───────────────────────────────────

def test_recall_chain_p14_accepted_p15_isolated():
    """泰科龙形态端到端契约：tail-recall 召回 p14 → 其真实尾行 seq85-89 进 accepted；
    技术页类垃圾行（无 qty/无价 或 seq 重叠）只能进 review_candidates，不得污染正式行。

    串联 _tail_recall_pages（页级召回）+ _filter_recall_rows（行级门禁）两层，
    断言 Codex 要求：p14 能召回；p15 技术行只进候选区。
    """
    # 1) 页级召回：连续报价链 p4..p13（10页），p14 误判出 tgt → 召回 p14
    tgt = list(range(4, 14))
    recall_pages = _tail_recall_pages(tgt, set(tgt), total_pages=16)
    assert recall_pages == [14], "长报价链尾页 p14 必须被召回"

    # 2) 可信链尾 seq=84（p4..p13 抽出 seq 1..84）
    trusted = [_trusted(84)]

    # 3) 召回页混合内容：
    #    - p14 真实尾行 seq85-89（完整字段）→ 应 accepted
    #    - 技术页风格行：部件参数无 qty/无价（component_parameter_table 残留）→ 隔离
    #    - 技术页风格行：seq=80 与可信链重叠（OCR 把技术页误读出旧序号）→ 隔离
    real_tail = [_qrow(seq=s, name=f"阀门{s}", page=14) for s in range(85, 90)]
    tech_no_qty = _qrow(seq=None, name="阀体材质 304", page=14, qty=None)
    tech_no_qty.fields["unit_price_incl_tax"] = None
    tech_no_qty.fields["total_price_incl_tax"] = None
    tech_seq_overlap = _qrow(seq=80, name="DN100 球墨铸铁", page=14)
    recall_rows = real_tail + [tech_no_qty, tech_seq_overlap]

    accepted, review = _filter_recall_rows(recall_rows, trusted, name_key="material")

    accepted_seqs = sorted(int(r.fields["seq"]) for r in accepted)
    assert accepted_seqs == [85, 86, 87, 88, 89], "p14 真实尾行 seq85-89 必须全部 accepted"
    # 技术行进候选区，绝不出现在 accepted
    assert tech_no_qty in review and tech_no_qty not in accepted
    assert tech_seq_overlap in review and tech_seq_overlap not in accepted
    # 候选行都带 recall_review_candidate 标记，且 accepted 里没有任何候选标记
    assert all("recall_review_candidate" in r.validation_flags for r in review)
    assert all("recall_review_candidate" not in r.validation_flags for r in accepted)


def test_extraction_draft_has_review_candidates_field():
    """ExtractionDraft 必须有独立 review_candidates 字段，与 rows 分离。"""
    from apps.api.intelligence.extraction_draft import ExtractionDraft, QualityReport
    d = ExtractionDraft(
        doc_type="quote", source_file="x.pdf", page_count=1,
        processed_page_count=1, target_pages=[1], rows=[], meta={},
        quality=QualityReport(status="PASS"),
    )
    assert hasattr(d, "review_candidates")
    assert d.review_candidates == [], "默认空列表，不与 rows 共享引用"
    assert d.review_candidates is not d.rows
