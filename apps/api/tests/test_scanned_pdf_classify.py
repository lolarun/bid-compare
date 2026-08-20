"""test_scanned_pdf_classify.py — design/29 §3 Tier 1.5。

跟本项目"unit tests(本地契约) vs fresh E2E(真实模型链路)"的证据类型区分
一致（CLAUDE.md §3）：原生 PDF 路径零模型调用，直接拿真实语料做单测；
扫描件路径用注入的假 call 验证**管线本身接得对**（渲染第一页、传给
call、解析返回值），不在这里断言真实模型准确率——那已经在 design/29 §3.1
测过、记录成"0/7，暂停"的真实发现，不是这份单测该覆盖的东西，也不应该
在单测里假装能覆盖。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.intelligence.scanned_pdf_classify import (
    Tier15Result,
    classify_native_pdf,
    classify_pdf_for_dispatch,
    classify_scanned_pdf,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")


# ── 原生 PDF：零模型调用，真实语料 ──────────────────────────────────────────

class TestNativePdfRealFixtures:
    @pytest.mark.parametrize("filename", [
        "金桥地体上盖招标文件.pdf",
        "prj2_电缆招标.pdf",
    ])
    def test_real_tender_pdf_classified_correctly(self, filename):
        path = DOCS / "tender" / filename
        _skip_if_missing(path)
        result = classify_native_pdf(str(path))
        assert result.verdict == "tender"
        assert result.method == "native_text_keyword"

    def test_cover_region_excludes_toc_false_positive(self):
        """design/29 §3.1 记录的真 bug 的回归测试：招标文件目录里列"第四章
        投标须知"这类章节名，本身带"投标"字样，扫全文（或前两页全文）会
        两侧关键词都命中、退化成 uncertain。判据必须限定在目录之前的封面
        区域——用真实语料验证，不是假设。"""
        path = DOCS / "tender" / "金桥地体上盖招标文件.pdf"
        _skip_if_missing(path)
        result = classify_native_pdf(str(path))
        assert result.verdict == "tender", (
            f"目录里的章节名污染了封面判据，退化成 {result.verdict}——"
            f"cover-region 截断没生效"
        )


# ── 扫描件：管线本身，注入假 call ────────────────────────────────────────────

class TestScannedPdfPipeline:
    """design/29 §3.1：真实模型准确率已经测过是 0/7，这里不重复断言准确率，
    只验证管线接线本身正确——渲染第一页、调用 call、把返回值原样映射进
    Tier15Result。"""

    def test_call_none_returns_uncertain_without_crashing(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        result = classify_scanned_pdf(str(path), call=None)
        assert result.verdict == "uncertain"
        assert result.method == "scanned_vl"

    def test_injected_call_result_passed_through(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        captured_images = []

        def fake_call(image_bytes: bytes) -> dict:
            captured_images.append(image_bytes)
            return {"doc_type": "bid", "project_name_hint": "某项目",
                    "supplier_name_hint": "某供应商", "evidence": ["测试证据"]}

        result = classify_scanned_pdf(str(path), call=fake_call)
        assert result.verdict == "bid"
        assert result.project_name_hint == "某项目"
        assert result.supplier_name_hint == "某供应商"
        assert len(captured_images) == 1  # 只送了一页，不是整份文档
        assert captured_images[0].startswith(b"\x89PNG")  # 真的渲染出了 PNG，不是空字节

    def test_uncertain_from_call_passes_through_honestly(self):
        """模型答"不确定"是合法答案，管线不能把它悄悄变成一个具体判定。"""
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)

        def fake_call(_image_bytes: bytes) -> dict:
            return {"doc_type": "uncertain", "project_name_hint": "",
                    "supplier_name_hint": "", "evidence": ["封面信息不足"]}

        result = classify_scanned_pdf(str(path), call=fake_call)
        assert result.verdict == "uncertain"


# ── 统一分派入口：文字层探测决定走哪条 ──────────────────────────────────────

class TestDispatchRouting:
    def test_native_pdf_routes_to_keyword_path_not_vl(self):
        path = DOCS / "tender" / "金桥地体上盖招标文件.pdf"
        _skip_if_missing(path)

        def should_not_be_called(_image_bytes: bytes) -> dict:
            raise AssertionError("原生 PDF 不应该走视觉调用路径")

        result = classify_pdf_for_dispatch(str(path), call=should_not_be_called)
        assert result.method == "native_text_keyword"

    def test_scanned_pdf_routes_to_vl_path(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        called = []

        def fake_call(image_bytes: bytes) -> dict:
            called.append(image_bytes)
            return {"doc_type": "bid", "project_name_hint": "", "supplier_name_hint": "", "evidence": []}

        result = classify_pdf_for_dispatch(str(path), call=fake_call)
        assert result.method == "scanned_vl"
        assert len(called) == 1
