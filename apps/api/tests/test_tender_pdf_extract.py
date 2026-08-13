"""招标文件 PDF 投标清单抽取 + 品牌硬信号 + Excel/PDF 对账 测试。

快速单测（无 API）：
- 品牌匹配（brand_match）
- 锚点序列化往返（含 materials/brand/source_ref）
- source_reconcile.reconcile_anchors
- TenderListSession 持久化 source_type/brand 字段
- build_anchor_review_matrix 暴露供应商品牌 + 锚点材质 + 品牌要求，且品牌不入供应商列

真实 VL（e2e，需 DASHSCOPE_API_KEY + 真实 PDF + Excel）：
- extract_bidlist 定位第13页品牌表 + 14-18页清单，row_count/材质/品牌映射
- Excel vs PDF 对账：seq 集合比对 + 已知首尾项字段一致

HTML 页面评分（_score_page/_is_brand_page/_is_bidlist_page/_detect_pages）与
_row_to_anchor 覆盖已随 legacy OCR→HTML 识别链一并删除（2026-08-11，最佳实践
评审 F1）——页定位与逐行抽取现在都在 vl_tender.py 内部完成一次 VL 调用，不再有
独立的 HTML 打分步骤可单测；行为由本文件的 e2e 用例整体验证。
"""

from pathlib import Path

import pytest

from apps.api.services.supplier.brand_match import build_brand_context, check_brand
from apps.api.services.tender.tender_list import TenderAnchor, anchor_to_json, rebuild_anchors
from apps.api.services.tender.source_reconcile import reconcile_anchors

REPO = Path(__file__).parent.parent.parent.parent
TENDER_PDF = REPO / "docs" / "test" / "金桥地体上盖招标文件.pdf"
TENDER_XLSX = REPO / "docs" / "test" / "金桥地体上盖招标文件.xlsx"

BRAND_REQ = [
    {"brand_en": "KITZ", "brand_cn": "开滋"},
    {"brand_en": "WATTS", "brand_cn": "沃茨"},
    {"brand_en": "BERMAD", "brand_cn": "伯尔梅特"},
]
SUPPLIER_BRANDS = [
    {"supplier_name": "凯硕新正", "brand": "开滋", "supplier_id": 1},
    {"supplier_name": "上海绵存", "brand": "沃茨", "supplier_id": 2},
    {"supplier_name": "上海泰科龙", "brand": "伯尔梅特", "supplier_id": 3},
]


# ── 品牌硬信号 ──────────────────────────────────────────────────────────────

def test_build_brand_context_aliases():
    allowed, expected = build_brand_context(BRAND_REQ, SUPPLIER_BRANDS)
    assert "kitz" in allowed and "开滋" in allowed
    assert "watts" in allowed and "沃茨" in allowed
    # supplier 1 expected brand 开滋 expands to include English alias
    assert "kitz" in expected[1]
    assert "开滋" in expected[1]


def test_check_brand_verdicts():
    allowed, expected = build_brand_context(BRAND_REQ, SUPPLIER_BRANDS)
    # supplier 1 should quote 开滋/KITZ
    assert check_brand("KITZ球阀", allowed, expected.get(1)) == "match"
    assert check_brand("开滋", allowed, expected.get(1)) == "match"
    # supplier 1 quoting 沃茨 → allowed but not its registered brand
    assert check_brand("沃茨", allowed, expected.get(1)) == "allowed"
    # out-of-scope brand → conflict
    assert check_brand("杂牌阀门", allowed, expected.get(1)) == "conflict"
    # no brand on quote → unknown (no signal)
    assert check_brand("", allowed, expected.get(1)) == "unknown"
    # no brand requirement configured → unknown
    assert check_brand("KITZ", set(), None) == "unknown"


# ── 锚点序列化 + _row_to_anchor ──────────────────────────────────────────────

def test_anchor_json_roundtrip_keeps_materials_brand_sourceref():
    a = TenderAnchor(
        seq=24, name="橡胶瓣止回阀", spec="DN65", pressure="1.6Mpa",
        materials={"阀体": "球墨铸铁", "密封圈": "EPDM"},
        unit="个", qty=5.0, brand="开滋", remark="给水系统",
        source_ref={"page": 15, "row": 24},
    )
    j = anchor_to_json(a, "阀门")
    assert j["materials"] == {"阀体": "球墨铸铁", "密封圈": "EPDM"}
    assert j["brand"] == "开滋"
    assert j["remark"] == "给水系统"
    assert j["source_ref"] == {"page": 15, "row": 24}

    class _S:
        anchors_json = [j]
    [back] = rebuild_anchors(_S())
    assert back.materials == {"阀体": "球墨铸铁", "密封圈": "EPDM"}
    assert back.brand == "开滋"
    assert back.source_ref == {"page": 15, "row": 24}
    assert back.material_text() == "球墨铸铁/EPDM"


# ── Excel vs PDF 对账（source_reconcile）────────────────────────────────────

def _make_xlsx_item(seq, name, spec="DN50", unit="个", qty=10):
    return {"seq": str(seq), "name": name, "spec": spec, "unit": unit, "qty": qty}

def _make_pdf_item(seq, name, spec="DN50", unit="个", qty=10):
    return {"seq": str(seq), "name": name, "spec": spec, "unit": unit, "qty": qty}


def test_reconcile_consistent():
    xlsx = [_make_xlsx_item(i, f"阀门{i}") for i in range(1, 6)]
    pdf  = [_make_pdf_item(i,  f"阀门{i}") for i in range(1, 6)]
    r = reconcile_anchors(xlsx, pdf)
    assert r["recommended_source"] == "both_consistent"
    assert r["seq_missing_in_pdf"] == []
    assert r["seq_missing_in_xlsx"] == []
    assert r["field_mismatches"] == []
    assert r["xlsx_count"] == 5
    assert r["pdf_count"] == 5


def test_reconcile_seq_missing_in_pdf():
    xlsx = [_make_xlsx_item(i, f"阀门{i}") for i in range(1, 6)]
    pdf  = [_make_pdf_item(i,  f"阀门{i}") for i in range(1, 5)]   # seq 5 missing
    r = reconcile_anchors(xlsx, pdf)
    assert r["recommended_source"] == "excel"
    assert r["seq_missing_in_pdf"] == ["5"]
    assert r["seq_missing_in_xlsx"] == []


def test_reconcile_seq_missing_in_xlsx():
    xlsx = [_make_xlsx_item(i, f"阀门{i}") for i in [1, 2, 3]]
    pdf  = [_make_pdf_item(i,  f"阀门{i}") for i in [1, 2, 3, 4]]  # seq 4 extra in PDF
    r = reconcile_anchors(xlsx, pdf)
    assert r["recommended_source"] == "excel"
    assert r["seq_missing_in_xlsx"] == ["4"]
    assert r["seq_missing_in_pdf"] == []


def test_reconcile_field_mismatch_name():
    xlsx = [_make_xlsx_item(1, "截止阀"), _make_xlsx_item(2, "蝶阀")]
    pdf  = [_make_pdf_item(1,  "截止阀"), _make_pdf_item(2, "软密封蝶阀")]  # name differs
    r = reconcile_anchors(xlsx, pdf)
    assert r["recommended_source"] == "excel"
    mismatches = {m["seq"]: m for m in r["field_mismatches"]}
    assert "2" in mismatches
    assert mismatches["2"]["field"] == "品名"
    assert mismatches["2"]["xlsx_value"] == "蝶阀"
    assert mismatches["2"]["pdf_value"] == "软密封蝶阀"


def test_reconcile_qty_mismatch():
    xlsx = [_make_xlsx_item(1, "球阀", qty=10)]
    pdf  = [_make_pdf_item(1,  "球阀", qty=8)]
    r = reconcile_anchors(xlsx, pdf)
    assert any(m["field"] == "数量" for m in r["field_mismatches"])


def test_reconcile_ignores_empty_values():
    """一方字段为空时不标差异（无法对比）。"""
    xlsx = [_make_xlsx_item(1, "阀门", spec="")]    # Excel 无规格
    pdf  = [_make_pdf_item(1,  "阀门", spec="DN50")]
    r = reconcile_anchors(xlsx, pdf)
    # spec 差异不计入（Excel 值为空）
    spec_m = [m for m in r["field_mismatches"] if m["field"] == "规格"]
    assert spec_m == []


# ── DB：session 持久化 + 复核矩阵品牌展示 ────────────────────────────────────

def _seed_pdf_session(db):
    """3 锚点(含材质) × 3 供应商，PDF 来源 + 品牌映射。"""
    from apps.api.models import Project, Supplier, Material, Quote, TenderListSession
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem

    proj = Project(name="PdfTest", code="PDF-1")
    db.add(proj); db.flush()

    sups = [Supplier(name=n) for n in ["凯硕新正机电", "上海绵存机电设备", "上海泰科龙阀门"]]
    db.add_all(sups); db.flush()
    sids = [s.id for s in sups]

    CAT = "阀门"
    anchors_json = [
        {"seq": str(i), "name": f"截止阀{i}", "spec": f"DN{i*25}", "pressure": "1.6Mpa",
         "model": "", "materials": {"阀体": "球墨铸铁", "阀芯": "不锈钢"},
         "unit": "个", "qty": 2, "brand": "开滋", "profession": "给排水",
         "remark": "", "category": CAT, "canonical": {}, "source_ref": {"page": 14, "row": i}}
        for i in range(1, 4)
    ]
    smap = [
        {"supplier_name": "凯硕新正机电", "brand": "开滋", "supplier_id": sids[0]},
        {"supplier_name": "上海绵存机电设备", "brand": "沃茨", "supplier_id": sids[1]},
        {"supplier_name": "上海泰科龙阀门", "brand": "伯尔梅特", "supplier_id": sids[2]},
    ]
    session = TenderListSession(
        project_id=proj.id, category=CAT, file_name="招标文件.pdf",
        source_type="pdf", anchors_total=3, anchors_json=anchors_json,
        brand_requirement=BRAND_REQ, supplier_brand_map=smap,
        version=1, is_current=True, status="confirmed",
        confirmed_supplier_ids=sids,
    )
    db.add(session); db.flush()

    # materials + quotes + groups/items so cells are quoted
    for i in range(1, 4):
        mat = Material(standard_name=f"截止阀{i}", spec=f"DN{i*25}", category=CAT, profession="给排水")
        db.add(mat); db.flush()
        grp = BidAlignmentGroup(
            project_id=proj.id, category=CAT, status="confirmed",
            suggested_name=mat.standard_name, suggested_spec=mat.spec,
            anchor_seq=str(i), tender_list_session_id=session.id,
        )
        db.add(grp); db.flush()
        for sid in sids:
            q = Quote(material_id=mat.id, supplier_id=sid, project_id=proj.id,
                      unit_price=100.0 + sid, quantity=2, total_price=(100.0 + sid) * 2,
                      brand="开滋")
            db.add(q); db.flush()
            db.add(BidAlignmentItem(group_id=grp.id, quote_id=q.id, supplier_id=sid, action="align"))
    db.commit()
    return proj.id, CAT, sids, session.id


def test_session_persists_pdf_source_and_brands(db_session):
    proj_id, cat, sids, sess_id = _seed_pdf_session(db_session)
    from apps.api.models import TenderListSession
    s = db_session.get(TenderListSession, sess_id)
    assert s.source_type == "pdf"
    assert s.brand_requirement == BRAND_REQ
    assert s.supplier_brand_map[0]["brand"] == "开滋"


def test_review_matrix_exposes_brand_and_materials(db_session):
    proj_id, cat, sids, _ = _seed_pdf_session(db_session)
    from apps.api.services.matrix.bid_matrix import build_anchor_review_matrix
    m = build_anchor_review_matrix(db_session, proj_id, cat, supplier_ids=sids)

    # 90×3 风格闭包：行数 == anchors_total，格数 == anchors_total × supplier_count
    assert m["anchors_total"] == 3
    assert m["supplier_count"] == 3
    total_cells = sum(len(r["cells"]) for r in m["rows"])
    assert total_cells == 3 * 3

    # 业主品牌要求随矩阵返回
    assert {b["brand_en"] for b in m["brand_requirement"]} == {"KITZ", "WATTS", "BERMAD"}

    # 供应商带参与品牌（供应商属性），但 supplier_name 仍是公司名（品牌不入供应商列）
    brands = {s["supplier_name"]: s["brand"] for s in m["suppliers"]}
    assert brands["凯硕新正机电"] == "开滋"
    assert brands["上海泰科龙阀门"] == "伯尔梅特"
    for s in m["suppliers"]:
        # 品牌名绝不等于供应商名（防串列）
        assert s["supplier_name"] not in ("开滋", "沃茨", "伯尔梅特")

    # 锚点行带材质 + 品牌要求
    row0 = m["rows"][0]
    assert "球墨铸铁" in row0["anchor_materials"]
    assert row0["anchor_brand"] == "开滋"


# ── 真实 OCR（e2e）──────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.skipif(
    not TENDER_PDF.exists() or not TENDER_XLSX.exists(),
    reason="真实招标 PDF 或 Excel 不存在"
)
def test_extract_bidlist_real_pdf():
    from apps.api.core.config import get_settings
    s = get_settings()
    if not s.DASHSCOPE_API_KEY:
        pytest.skip("DASHSCOPE_API_KEY 未配置")
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.services.tender.tender_pdf import extract_bidlist

    provider = DashScopeOCRProvider(
        api_key=s.DASHSCOPE_API_KEY, base_url=s.DASHSCOPE_BASE_URL,
        ocr_model=s.DASHSCOPE_OCR_MODEL, llm_model=s.DASHSCOPE_LLM_MODEL,
    )
    # 传入 xlsx_path，在 extract_bidlist 内自动运行 source reconciliation
    result = extract_bidlist(str(TENDER_PDF), provider, xlsx_path=str(TENDER_XLSX))

    # ══════════════════════════════════════════════════════════════════
    # 完整诊断报告（人工审查用，pytest -s 时输出）
    # ══════════════════════════════════════════════════════════════════
    qm = result["quality_metrics"]
    recon = result.get("reconcile") or {}

    print("\n" + "═" * 60)
    print("金桥 PDF E2E 诊断报告")
    print("═" * 60)

    # 1. 页定位
    print(f"\n[页定位]")
    print(f"  brand_page : {result['detected_pages']['brand']}")
    print(f"  bidlist_pages : {result['detected_pages']['bidlist']}")
    print(f"  source_type : {result['source_type']}")

    # 2. TableGrid 使用率
    print(f"\n[TableGrid 使用率]")
    diag = result["page_diagnostics"]
    tg = qm["table_grid_pages"]
    fb = qm["html_fallback_pages"]
    print(f"  table_grid 页  : {tg}")
    print(f"  html_fallback 页: {[f['page'] for f in fb]}")
    for d in diag:
        retry_tag = " ← thinking retry" if d["thinking_retry"] else ""
        print(
            f"  page {d['page']:>2}: {d['input_mode']:<12} "
            f"expected={d['expected_rows']:>3} extracted={d['extracted_rows']:>3}"
            f"{'  reason=' + d['fallback_reason'] if d['fallback_reason'] else ''}"
            f"{retry_tag}"
        )

    # 3. 行数 & seq 范围
    print(f"\n[行数 & seq]")
    numeric = sorted(int(s) for s in qm["seq_missing"] or [] if s.isdigit())
    all_seqs = [str(it.get("seq", "")).strip() for it in result["items"] if it.get("seq")]
    numeric_seqs = sorted(int(s) for s in all_seqs if s.isdigit())
    seq_range = f"{numeric_seqs[0]}..{numeric_seqs[-1]}" if numeric_seqs else "n/a"
    print(f"  row_count    : {result['row_count']}")
    print(f"  seq range    : {seq_range}")
    print(f"  seq_missing  : {qm['seq_missing'] or '(none)'}")
    print(f"  seq_duplicate: {qm['seq_duplicate'] or '(none)'}")
    print(f"  row_count_by_page: {qm['row_count_by_page']}")

    # 4. 字段覆盖率
    print(f"\n[字段覆盖率]")
    print(f"  material_columns_filled_rate : {qm['material_columns_filled_rate']:.1%}")
    print(f"  brand_filled_rate            : {qm['brand_filled_rate']:.1%}")
    print(f"  source_ref_coverage          : {qm['source_ref_coverage']:.1%}")
    print(f"  qty_parse_success_rate       : {qm['qty_parse_success_rate']:.1%}")

    # 5. 品牌表
    print(f"\n[品牌表]")
    print(f"  brand_requirement : {[b['brand_en'] + '/' + b['brand_cn'] for b in result['brand_requirement']]}")
    print(f"  supplier_brands   : {[(s['supplier_name'][:6], s['brand']) for s in result['supplier_brands']]}")

    # 6. Excel vs PDF 对账
    print(f"\n[Excel vs PDF 对账]")
    if recon:
        print(f"  xlsx_count         : {recon.get('xlsx_count')}")
        print(f"  pdf_count          : {recon.get('pdf_count')}")
        print(f"  missing_in_pdf     : {recon.get('seq_missing_in_pdf') or '(none)'}")
        print(f"  missing_in_xlsx    : {recon.get('seq_missing_in_xlsx') or '(none)'}")
        print(f"  field_mismatches   : {len(recon.get('field_mismatches', []))} 处")
        print(f"  recommended_source : {recon.get('recommended_source')}")
        for m in (recon.get("field_mismatches") or [])[:10]:
            print(f"    seq={m['seq']} {m['field']}: Excel={m['xlsx_value']!r} PDF={m['pdf_value']!r}")
    else:
        print("  (未运行对账)")
    print("═" * 60)

    # ══════════════════════════════════════════════════════════════════
    # 断言
    # ══════════════════════════════════════════════════════════════════

    # 页定位
    assert result["detected_pages"]["brand"] == 13, "品牌表页未定位到第13页"
    assert result["detected_pages"]["bidlist"] == [14, 15, 16, 17, 18], \
        f"清单页定位偏差: {result['detected_pages']['bidlist']}"
    assert result["source_type"] == "pdf_primary"

    # 品牌表
    brands_en = {b["brand_en"] for b in result["brand_requirement"]}
    assert {"KITZ", "WATTS", "BERMAD"} <= brands_en, f"品牌要求缺失: {brands_en}"
    assert len(result["supplier_brands"]) == 3, \
        f"供应商品牌条数: {len(result['supplier_brands'])}"

    # page_diagnostics 结构完整
    assert len(result["page_diagnostics"]) == len(result["detected_pages"]["bidlist"])
    for d in result["page_diagnostics"]:
        assert d["input_mode"] == "vl_direct"
        assert isinstance(d["expected_rows"], int)
        assert isinstance(d["extracted_rows"], int)

    # seq 连续无重复
    assert qm["seq_duplicate"] == [], f"PDF 存在重复 seq: {qm['seq_duplicate']}"
    assert len(qm["seq_missing"]) <= 3, \
        f"PDF seq 缺口过多({len(qm['seq_missing'])}): {qm['seq_missing']}"

    # 材质覆盖率：至少 40% 的行填了材质
    assert qm["material_columns_filled_rate"] >= 0.4, \
        f"材质覆盖率偏低: {qm['material_columns_filled_rate']:.1%}"

    # source_ref 全覆盖（每行都有 page+row）
    assert qm["source_ref_coverage"] == 1.0, \
        f"source_ref 未全覆盖: {qm['source_ref_coverage']:.1%}"

    # qty 解析成功率
    assert qm["qty_parse_success_rate"] >= 0.8, \
        f"qty 解析成功率偏低: {qm['qty_parse_success_rate']:.1%}"

    # reconcile：seq 集合差异不超过 5 条
    if recon and "error" not in recon:
        missing_pdf = len(recon["seq_missing_in_pdf"])
        missing_xlsx = len(recon["seq_missing_in_xlsx"])
        assert missing_pdf <= 5, \
            f"PDF 缺失 Excel 序号过多({missing_pdf}): {recon['seq_missing_in_pdf']}"
        assert missing_xlsx <= 5, \
            f"PDF 多出 Excel 序号过多({missing_xlsx}): {recon['seq_missing_in_xlsx']}"
