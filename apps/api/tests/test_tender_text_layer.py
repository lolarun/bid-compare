"""docs/design/25（轨A）：招标采购清单文字层直抽测试。

快速单测（无 API，全部对着真实文字层 PDF 结构走）：
- has_usable_text_layer：金桥（有文字层）True，扫描件 False
- build_anchor_csv：89 明细行 + 1 总计行、跨页续表（14-18页无自带表头）拼接正确、
  两级表头拍平「材质_阀体」等
- build_brand_requirements：业主品牌要求 + 各投标单位参与品牌，与
  test_tender_pdf_extract.py 的 BRAND_REQ/SUPPLIER_BRANDS 同构
- 回落判据：无文字层的扫描件、有文字层但没有清单表的文档都返回 None
- parser_mode 标注为 "text_layer"，不冒充 "vl_direct"（评审 N1）

真实 VL（e2e，需 DASHSCOPE_API_KEY）：
- parse_tender_document_text_layer 端到端（含封面标量的小 VL 调用）
- 与 parse_tender_document（真·VL-direct）逐字段对照：89 行 anchor 首尾一致、
  meta 四标量一致、brand_requirement/supplier_brands 一致——这是 design/25 §5
  验收标准的自动化版本，本轮实现时已手工跑过一次（约 364s → 14-18s，~20-25倍）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent.parent
TENDER_PDF = REPO / "tests" / "fixtures" / "documents" / "tender" / "金桥地体上盖招标文件.pdf"
SCANNED_BID_PDF = REPO / "tests" / "fixtures" / "documents" / "bid" / "泰科龙投标文件.pdf"          # 纯扫描件，无文字层
TEXT_LAYER_NO_TABLE_PDF = REPO / "docs" / "test1" / "prj2" / "附件三：合同文本固定样式.pdf"  # design/28 §6：未迁移文件

BRAND_REQ = [
    {"brand_en": "KITZ", "brand_cn": "开滋"},
    {"brand_en": "WATTS", "brand_cn": "沃茨"},
    {"brand_en": "BERMAD", "brand_cn": "伯尔梅特"},
]


def _require_fixture(path: Path):
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")


# ─── §1 文字层检测 ───────────────────────────────────────────────────────────

def test_has_usable_text_layer_true_on_born_digital_pdf():
    _require_fixture(TENDER_PDF)
    from apps.api.intelligence.tender_text_layer import has_usable_text_layer
    assert has_usable_text_layer(str(TENDER_PDF)) is True


def test_has_usable_text_layer_false_on_scan():
    _require_fixture(SCANNED_BID_PDF)
    from apps.api.intelligence.tender_text_layer import has_usable_text_layer
    assert has_usable_text_layer(str(SCANNED_BID_PDF)) is False


def test_has_usable_text_layer_missing_file_returns_false_not_raise():
    from apps.api.intelligence.tender_text_layer import has_usable_text_layer
    assert has_usable_text_layer(str(REPO / "tests" / "fixtures" / "documents" / "tender" / "不存在.pdf")) is False


# ─── §2 采购清单表：跨页续表 + 两级表头拍平 ────────────────────────────────────

def test_build_anchor_csv_covers_all_89_rows_across_5_pages():
    """89 行分布在 14-18 共 5 页，只有第 14 页有表头行——15-18 页续表沿用。"""
    _require_fixture(TENDER_PDF)
    import pdfplumber
    from apps.api.intelligence.tender_text_layer import build_anchor_csv

    with pdfplumber.open(str(TENDER_PDF)) as pdf:
        result = build_anchor_csv(pdf, len(pdf.pages))
    assert result is not None
    csv_text, pages = result
    assert pages == [14, 15, 16, 17, 18]

    lines = csv_text.splitlines()
    assert "材质_阀体" in lines[0] and "材质_阀芯" in lines[0]   # 两级表头拍平
    seqs = [line.split(",")[1] for line in lines[1:] if line.split(",")[1].isdigit()]
    assert seqs == [str(i) for i in range(1, 90)]               # 序号 1..89 完整连续


def test_build_anchor_csv_none_when_no_anchor_table():
    _require_fixture(TEXT_LAYER_NO_TABLE_PDF)
    import pdfplumber
    from apps.api.intelligence.tender_text_layer import build_anchor_csv

    with pdfplumber.open(str(TEXT_LAYER_NO_TABLE_PDF)) as pdf:
        assert build_anchor_csv(pdf, len(pdf.pages)) is None


def test_continuation_requires_consecutive_pages():
    """跳页的表格即使列数凑巧一致，也不当续页吞并——见 build_anchor_csv 里
    last_page+1 的判据。用合成表格直接测这条边界，不依赖真实夹具凑出这个情形。"""
    from apps.api.intelligence.tender_text_layer import _table_to_anchor_csv_rows

    header_table = [
        ["序号", "项目名称", "规格", "单位", "数量"],
        ["1", "闸阀", "DN50", "个", "10"],
    ]
    flat_header, rows = _table_to_anchor_csv_rows(header_table, page_num=1)
    assert flat_header == ["序号", "项目名称", "规格", "单位", "数量"]
    assert len(rows) == 1

    # 续页格式对但没提供 carried_header → 应判不是清单表
    continuation_no_carry = [["2", "截止阀", "DN25", "个", "5"]]
    flat_header2, rows2 = _table_to_anchor_csv_rows(continuation_no_carry, page_num=2)
    assert flat_header2 is None and rows2 == []

    # 提供 carried_header 且列数一致、首格是数字 → 续页
    flat_header3, rows3 = _table_to_anchor_csv_rows(
        continuation_no_carry, page_num=2, carried_header=flat_header)
    assert flat_header3 == flat_header
    assert len(rows3) == 1


# ─── §3 品牌要求表 ───────────────────────────────────────────────────────────

def test_build_brand_requirements_matches_known_shape():
    _require_fixture(TENDER_PDF)
    import pdfplumber
    from apps.api.intelligence.tender_text_layer import build_brand_requirements

    with pdfplumber.open(str(TENDER_PDF)) as pdf:
        brand_requirement, supplier_brands, brand_page = build_brand_requirements(pdf)

    assert brand_requirement == BRAND_REQ
    assert brand_page == 13
    assert len(supplier_brands) == 3
    assert {s["brand"] for s in supplier_brands} == {"开滋", "沃茨", "伯尔梅特"}


def test_build_brand_requirements_empty_when_absent():
    _require_fixture(TEXT_LAYER_NO_TABLE_PDF)
    import pdfplumber
    from apps.api.intelligence.tender_text_layer import build_brand_requirements

    with pdfplumber.open(str(TEXT_LAYER_NO_TABLE_PDF)) as pdf:
        brand_requirement, supplier_brands, brand_page = build_brand_requirements(pdf)
    assert brand_requirement == [] and supplier_brands == [] and brand_page is None


# ─── §4 parser_mode 标注（评审 N1：标签必须诚实反映来源） ─────────────────────

def test_build_tender_draft_honors_parser_mode():
    from apps.api.intelligence.vl_tender import build_tender_draft

    csv_text = "row_type,序号,项目名称,规格,单位,数量,page\ndetail,1,闸阀,DN50,个,10,1"
    draft = build_tender_draft(
        csv_text, file_path="t.pdf", page_count=1, processed_pages=[1],
        parser_mode="text_layer",
    )
    assert draft.meta["recognizer"] == "text_layer"
    row = next(r for r in draft.rows if r.row_type == "quote_line")
    assert row.fields["parser_mode"] == "text_layer"

    # 默认值不变——quote 侧现有调用点不传这个参数时行为不受影响
    draft_default = build_tender_draft(
        csv_text, file_path="t.pdf", page_count=1, processed_pages=[1])
    assert draft_default.meta["recognizer"] == "vl_direct"


# ─── §5 e2e：与真·VL-direct 逐字段对照（design/25 §5 验收标准） ────────────────

@pytest.mark.e2e
def test_text_layer_matches_vl_direct_field_for_field():
    _require_fixture(TENDER_PDF)
    from apps.api.core.config import get_settings
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.intelligence.extraction_draft import DETAIL_ROW_TYPE
    from apps.api.intelligence.tender_text_layer import parse_tender_document_text_layer
    from apps.api.intelligence.vl_tender import parse_tender_document
    from apps.api.services.tender.tender_pdf import _draft_row_to_anchor

    s = get_settings()
    prov = DashScopeOCRProvider()

    def vl_call(imgs, prompt):
        return prov.vl_extract_csv(imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL)

    text_layer_result = parse_tender_document_text_layer(str(TENDER_PDF), vl_call=vl_call)
    assert text_layer_result is not None

    vl_result = parse_tender_document(
        str(TENDER_PDF), vl_call=vl_call,
        orient_call=lambda parts, prompt: prov.vl_extract_csv(
            [b for _t, b in parts], prompt,
            model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
    )

    tl_rows = [r for r in text_layer_result.draft.rows if r.row_type == DETAIL_ROW_TYPE]
    vl_rows = [r for r in vl_result.draft.rows if r.row_type == DETAIL_ROW_TYPE]
    assert len(tl_rows) == len(vl_rows) == 89

    tl_first, vl_first = _draft_row_to_anchor(tl_rows[0]), _draft_row_to_anchor(vl_rows[0])
    assert (tl_first.seq, tl_first.name, tl_first.spec, tl_first.unit, tl_first.qty) == \
           (vl_first.seq, vl_first.name, vl_first.spec, vl_first.unit, vl_first.qty)
    tl_last, vl_last = _draft_row_to_anchor(tl_rows[-1]), _draft_row_to_anchor(vl_rows[-1])
    assert (tl_last.seq, tl_last.name, tl_last.spec, tl_last.unit, tl_last.qty) == \
           (vl_last.seq, vl_last.name, vl_last.spec, vl_last.unit, vl_last.qty)

    assert text_layer_result.meta == vl_result.meta
    assert text_layer_result.requirements["brand_requirement"] == \
           vl_result.requirements["brand_requirement"]
