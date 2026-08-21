"""design/28 §3 Tier 0 分类器——对全量 MANIFEST 语料的验收测试。

MANIFEST.md 是标注答案集，不是这份测试自己的私有夹具清单：新增/替换语料时
先改 MANIFEST，再改这里，两边必须一直对得上，不然"验收覆盖了全量语料"这
句话就是假的。

金桥招标 xlsx 的"不确定"不是失败用例、是通过用例（design/28 §2）——它是
唯一一个 verdict 断言为 "uncertain" 的样本，别的样本判成 uncertain 才是
真失败。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.intelligence.document_classify import (
    classify_excel,
    classify_pdf,
    classify_tier0,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")


# ── Excel：definitive / strong / ambiguous 三档都要覆盖真实语料 ────────────

EXCEL_CASES = [
    # (相对路径, 期望 verdict, 期望 confidence, 备注)
    ("徐汇区华泾镇项目-采购清单.xlsx", "tender_list", "definitive",
     "0 价格列——空白清单表的定义性特征"),
    ("金桥地体上盖项目-采购清单.xlsx", "uncertain", "ambiguous",
     "design/28 §2 的验收样本：价格列混合信号（部分列全空、部分列全 0），"
     "本该判不确定，判死了才是回归"),
    ("金桥地体上盖项目-凯硕新正报价清单.xlsx", "bid_list", "strong", "design/28 §2 引用的 100% 参照"),
    ("金桥地体上盖项目-上海绵存报价清单.xlsx", "bid_list", "strong", ""),
    ("金桥地体上盖项目-泰科龙报价清单.xlsx", "bid_list", "strong", ""),
]


@pytest.mark.parametrize("rel_path,expected_verdict,expected_confidence,note", EXCEL_CASES)
def test_excel_tier0_matches_manifest(rel_path, expected_verdict, expected_confidence, note):
    path = DOCS / rel_path
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.verdict == expected_verdict, (
        f"{rel_path}: 期望 verdict={expected_verdict}，实得 {result.verdict}"
        f"（fill_rate={result.fill_rate}，price_columns={result.price_columns}）。{note}"
    )
    assert result.confidence == expected_confidence


def test_excel_uncertain_never_guesses_a_definitive_verdict():
    """红线（design/28 §5 red line 3）：低置信度必须标注，不能靠某次运气
    对上真实答案就悄悄升级成 definitive/strong——用金桥这份反复跑几次，
    verdict/confidence 必须稳定，不能因为浮点比较边界抖动。"""
    path = DOCS / "金桥地体上盖项目-采购清单.xlsx"
    _skip_if_missing(path)
    results = [classify_excel(str(path)) for _ in range(3)]
    assert all(r.verdict == "uncertain" for r in results)
    assert all(r.confidence == "ambiguous" for r in results)
    assert len({r.fill_rate for r in results}) == 1, "同一份文件反复跑，填充率必须确定性一致"


def test_excel_definitive_has_no_price_columns():
    path = DOCS / "徐汇区华泾镇项目-采购清单.xlsx"
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.price_columns == []
    assert result.fill_rate is None  # 没有价格列，填充率概念本身不适用，不是 0.0


def test_excel_strong_fill_rate_above_threshold():
    from apps.api.intelligence.document_classify import FILL_RATE_STRONG
    path = DOCS / "金桥地体上盖项目-凯硕新正报价清单.xlsx"
    _skip_if_missing(path)
    result = classify_excel(str(path))
    assert result.fill_rate is not None and result.fill_rate >= FILL_RATE_STRONG
    assert len(result.price_columns) >= 1


# ── PDF：Tier 0 只给文字层信号，不判招标/投标 ──────────────────────────────

PDF_TEXT_LAYER_CASES = [
    ("金桥地体上盖项目-招标文件.pdf", "native", "文本型招标 PDF（E2E_FIXTURES.md 记录 ~11771 字）"),
    ("徐汇区华泾镇项目-招标文件.pdf", "native", ""),
    ("金桥地体上盖项目-上海绵存投标文件.pdf", "scanned", "纯扫描件，无文字层"),
    ("金桥地体上盖项目-凯硕新正投标文件.pdf", "scanned", ""),
    ("金桥地体上盖项目-泰科龙投标文件.pdf", "scanned", "53 页扫描件，另有转置表/旋转已知坑，见 MANIFEST"),
    ("徐汇区华泾镇项目-上海浦东投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-亨通投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-宏胜投标文件.pdf", "scanned", ""),
    ("徐汇区华泾镇项目-远东投标文件.pdf", "scanned", ""),
]


@pytest.mark.parametrize("rel_path,expected_layer,note", PDF_TEXT_LAYER_CASES)
def test_pdf_tier0_text_layer_matches_manifest(rel_path, expected_layer, note):
    path = DOCS / rel_path
    _skip_if_missing(path)
    result = classify_pdf(str(path))
    assert result.text_layer == expected_layer, (
        f"{rel_path}: 期望 text_layer={expected_layer}，实得 {result.text_layer}。{note}"
    )


def test_pdf_tier0_never_decides_tender_vs_bid():
    """Tier 0 对 PDF 的输出类型本身就不含"招标/投标"这个字段——这条测试
    与其说是断言行为，不如说是锁定 schema：PdfClassification 只能长出
    text_layer 这类结构信号，谁往这个 dataclass 上加 verdict 字段就是在
    悄悄把 Tier 1/2 的职责挪进 Tier 0，必须先改设计文档再改代码。"""
    from apps.api.intelligence.document_classify import PdfClassification
    field_names = {f for f in PdfClassification.__dataclass_fields__}
    assert "verdict" not in field_names
    assert "type" not in field_names


# ── classify_tier0：扩展名分派 + MANIFEST 全量覆盖 ─────────────────────────

def test_classify_tier0_dispatches_by_extension():
    xlsx = DOCS / "金桥地体上盖项目-凯硕新正报价清单.xlsx"
    pdf = DOCS / "金桥地体上盖项目-凯硕新正投标文件.pdf"
    _skip_if_missing(xlsx)
    _skip_if_missing(pdf)
    from apps.api.intelligence.document_classify import ExcelClassification, PdfClassification
    assert isinstance(classify_tier0(str(xlsx)), ExcelClassification)
    assert isinstance(classify_tier0(str(pdf)), PdfClassification)


def test_classify_tier0_unsupported_extension_returns_none(tmp_path):
    p = tmp_path / "not_a_document.txt"
    p.write_text("irrelevant")
    assert classify_tier0(str(p)) is None


def test_manifest_corpus_full_coverage():
    """cut 2 的验收口径（design/28 §7）：unit tests over the whole MANIFEST
    corpus。用代码列出 MANIFEST.md 记录的全部 14 份文件（xlsx 5 份 + pdf 9
    份，2026-08-21 起改为扁平命名，直接放在 documents/ 下，不再分子目录），
    逐一跑 Tier 0，不遗漏、不静默跳过。"""
    all_files = sorted(DOCS.glob("*.xlsx")) + sorted(DOCS.glob("*.xls")) + sorted(DOCS.glob("*.pdf"))
    if not all_files:
        pytest.skip("MANIFEST 语料目录不存在（未在这套环境里签出真实夹具）")
    assert len(all_files) == 14, (
        f"MANIFEST 记录 14 份文件，目录下实际找到 {len(all_files)} 份：{[f.name for f in all_files]}。"
        "语料增减必须先改 MANIFEST.md 再改这条断言，不能悄悄漂移。"
    )
    for f in all_files:
        result = classify_tier0(str(f))
        assert result is not None, f"{f} 分类返回 None——扩展名分派没覆盖到这份真实语料"
