"""test_scanned_pdf_classify.py — design/29 §3 Tier 1.5。

跟本项目"unit tests(本地契约) vs fresh E2E(真实模型链路)"的证据类型区分
一致（CLAUDE.md §3）：原生 PDF 路径零模型调用，直接拿真实语料做单测；
扫描件路径的 pipeline-wiring 用注入的假 call 验证**管线本身接得对**
（渲染前几页、传给 call、解析返回值），不断言真实模型准确率——那是下面
`TestScannedPdfRealAccuracy`（`@pytest.mark.e2e`，默认不跑）的职责，两类
证据不互相冒充。

**2026-08-21 修正**：最初版本（只送第一页缩略图）实测 0/7，记在 git 历史
里但已经**不是当前实现**——送前几页原生分辨率图 + 点破常见易错点的提示词
后复测 8/8，`TestScannedPdfRealAccuracy` 就是这次复测的可重放版本，不是
把旧发现留着当"当前状态"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.intelligence.scanned_pdf_classify import (
    SCANNED_CLASSIFY_PAGES,
    Tier15Result,
    classify_native_pdf,
    classify_pdf_for_dispatch,
    classify_scanned_pdf,
    get_scanned_classify_call,
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
    """管线接线本身正确性——渲染前 `SCANNED_CLASSIFY_PAGES` 页、调用 call、
    把返回值原样映射进 Tier15Result。不断言真实模型准确率（见下面
    TestScannedPdfRealAccuracy）。"""

    def test_call_none_returns_uncertain_without_crashing(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        result = classify_scanned_pdf(str(path), call=None)
        assert result.verdict == "uncertain"
        assert result.method == "scanned_vl"

    def test_injected_call_result_passed_through(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        captured_batches = []

        def fake_call(images: list[bytes]) -> dict:
            captured_batches.append(images)
            return {"doc_type": "bid", "project_name_hint": "某项目",
                    "supplier_name_hint": "某供应商", "evidence": ["测试证据"]}

        result = classify_scanned_pdf(str(path), call=fake_call)
        assert result.verdict == "bid"
        assert result.project_name_hint == "某项目"
        assert result.supplier_name_hint == "某供应商"
        assert len(captured_batches) == 1  # call 只被调一次，一次性传入整批页
        images = captured_batches[0]
        assert 1 <= len(images) <= SCANNED_CLASSIFY_PAGES  # 送了前几页，不是整份文档
        assert all(img.startswith(b"\x89PNG") for img in images)  # 真的渲染出了 PNG，不是空字节

    def test_uncertain_from_call_passes_through_honestly(self):
        """模型答"不确定"是合法答案，管线不能把它悄悄变成一个具体判定。"""
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)

        def fake_call(_images: list[bytes]) -> dict:
            return {"doc_type": "uncertain", "project_name_hint": "",
                    "supplier_name_hint": "", "evidence": ["封面信息不足"]}

        result = classify_scanned_pdf(str(path), call=fake_call)
        assert result.verdict == "uncertain"


# ── 扫描件：真实模型准确率，fresh E2E（默认不跑，见 CLAUDE.md §3 证据分层）──

class TestScannedPdfRealAccuracy:
    """design/29 §3.1 记录的 0/7 已经是历史——2026-08-21 改成送前几页原生
    分辨率图 + 点破常见易错点的提示词后复测 8/8。这是那次复测的可重放版本，
    不是又一次临时脚本；需要真实 DASHSCOPE_API_KEY，默认用 `-m 'not e2e'`
    跳过，CI/日常跑单测不受影响。"""

    @pytest.mark.e2e
    @pytest.mark.parametrize("filename,expected", [
        ("prj1_上海浦东.pdf", "bid"), ("prj1_亨通.pdf", "bid"),
        ("prj1_宏胜.pdf", "bid"), ("prj1_远东.pdf", "bid"),
        ("上海绵存投标文件.pdf", "bid"), ("凯硕新正投标文件.pdf", "bid"),
        ("泰科龙投标文件.pdf", "bid"),
    ])
    def test_real_bid_pdf_classified_correctly(self, filename, expected):
        path = DOCS / "bid" / filename
        _skip_if_missing(path)
        call = get_scanned_classify_call()
        if call is None:
            pytest.skip("DASHSCOPE_API_KEY 未配置，无法跑真实视觉调用")
        result = classify_scanned_pdf(str(path), call=call)
        assert result.verdict == expected, (
            f"{filename}: 期望 {expected}，实际 {result.verdict}，"
            f"依据={result.evidence}"
        )

    @pytest.mark.e2e
    def test_real_tender_pdf_scanned_path_classified_correctly(self):
        """招标 PDF 走扫描件路径（不是原生文字层路径）时的真实判定——
        跟 TestNativePdfRealFixtures 测的是同一份文档不同判据路径，两条
        都要对。"""
        path = DOCS / "tender" / "金桥地体上盖招标文件.pdf"
        _skip_if_missing(path)
        call = get_scanned_classify_call()
        if call is None:
            pytest.skip("DASHSCOPE_API_KEY 未配置，无法跑真实视觉调用")
        result = classify_scanned_pdf(str(path), call=call)
        assert result.verdict == "tender", (
            f"期望 tender，实际 {result.verdict}，依据={result.evidence}"
        )


# ── 统一分派入口：文字层探测决定走哪条 ──────────────────────────────────────

class TestDispatchRouting:
    def test_native_pdf_routes_to_keyword_path_not_vl(self):
        path = DOCS / "tender" / "金桥地体上盖招标文件.pdf"
        _skip_if_missing(path)

        def should_not_be_called(_images: list[bytes]) -> dict:
            raise AssertionError("原生 PDF 不应该走视觉调用路径")

        result = classify_pdf_for_dispatch(str(path), call=should_not_be_called)
        assert result.method == "native_text_keyword"

    def test_scanned_pdf_routes_to_vl_path(self):
        path = DOCS / "bid" / "泰科龙投标文件.pdf"
        _skip_if_missing(path)
        called = []

        def fake_call(images: list[bytes]) -> dict:
            called.append(images)
            return {"doc_type": "bid", "project_name_hint": "", "supplier_name_hint": "", "evidence": []}

        result = classify_pdf_for_dispatch(str(path), call=fake_call)
        assert result.method == "scanned_vl"
        assert len(called) == 1
