"""design/28 cut 7——把 §4 的三组场景(A/B/C)接成可独立跑的 E2E 用例。

每组场景断言 MANIFEST.md 记录的每一份真实文件，Tier 0/1 分类链路给出的
判定跟标注答案一致——这是"分类器在真实语料上真的работает"的收口证据，
不是又一遍重复 test_document_classify.py/test_tier1_signals.py 已经测过
的单点判据（那两份测的是"判据本身对不对"，这里测的是"按场景分组、走完
整条链路，结果仍然对得上"）。

场景 B（全 Excel）是设计文档明确点名的优先场景（§4："zero model calls
end-to-end; any failure here is unambiguously in the business layer, not
the engine"）——这组测试完全不碰任何识别产物，只有 Tier 0，是三组里最
干净的一组，放在最前面。

场景 C 的四份投标 PDF（prj1_浦东/亨通/宏胜/远东）没有提交进仓库的识别
产物 JSON 可以重放——它们的 Tier 1 判定已经在 design/27 步骤5的真实回归
里跑过一遍真实数据（commit 8092525，263/132/132/139 行，供应商名/价格
字段都真实抽到），但那次的完整识别产物没有落成夹具文件保存下来。这里
如实标注这个缺口（`pytest.skip` 说明原因），不用编造的 job_result 硬凑
一份"看起来能测"的假数据——CLAUDE.md 的红线是"标准答案不能循环验证/
不能拿看似合理的数据冒充真实标注"，编一份结构对但内容假的 fixture 属于
这一类问题，宁可少测一块、明确写清楚为什么没测。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api.intelligence.document_classify import classify_excel, classify_pdf
from apps.api.intelligence.tier1_signals import classify_tier1

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"
LIVE_FIXTURES = Path(__file__).parent / "fixtures"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")


def _load_live(name: str) -> dict:
    path = LIVE_FIXTURES / name
    if not path.exists():
        pytest.skip(f"识别产物夹具缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ── 场景 B：金桥 all-Excel —— design/28 §4 明确点名的优先场景，零模型调用 ──

class TestScenarioB_AllExcel:
    """招标侧：金桥地体上盖招标文件.xlsx（刻意保留的 ambiguous 验收样本）+
    投标侧：绵存/凯硕/泰科龙投标清单.xlsx（均为 strong bid_list）。"""

    def test_tender_side_is_ambiguous_by_design(self):
        path = DOCS / "tender_list/金桥地体上盖招标文件.xlsx"
        _skip_if_missing(path)
        result = classify_excel(str(path))
        assert result.verdict == "uncertain"
        assert result.confidence == "ambiguous"

    @pytest.mark.parametrize("filename", [
        "上海绵存投标清单.xlsx", "凯硕新正投标清单.xlsx", "泰科龙投标清单.xlsx",
    ])
    def test_bid_side_is_strong_bid_list(self, filename):
        path = DOCS / "bid_list" / filename
        _skip_if_missing(path)
        result = classify_excel(str(path))
        assert result.verdict == "bid_list"
        assert result.confidence == "strong"


# ── 场景 A：金桥 all-PDF —— 扫描识别 + Paddle + 质量门全链路 ──────────────

class TestScenarioA_AllPdf:
    """Tier 0（文字层）+ Tier 1（识别产物真实夹具）两级都有真实数据可测。"""

    def test_tender_pdf_tier0_native_text_layer(self):
        path = DOCS / "tender/金桥地体上盖招标文件.pdf"
        _skip_if_missing(path)
        result = classify_pdf(str(path))
        assert result.text_layer == "native"

    def test_tender_pdf_tier1_strong_tender(self):
        job_result = _load_live("live_jingqiao_tender_result.json")
        result = classify_tier1(job_result)
        assert result.verdict == "tender"
        assert result.confidence == "strong"

    @pytest.mark.parametrize("pdf_name,fixture_name", [
        ("上海绵存投标文件.pdf", "live_shanghaimiancun_quote_result.json"),
        ("凯硕新正投标文件.pdf", "live_kaishuoxinzheng_quote_result.json"),
        ("泰科龙投标文件.pdf", "live_taikelong_quote_result.json"),
    ])
    def test_bid_pdf_tier0_scanned_then_tier1_strong_bid(self, pdf_name, fixture_name):
        path = DOCS / "bid" / pdf_name
        _skip_if_missing(path)
        tier0 = classify_pdf(str(path))
        assert tier0.text_layer == "scanned"

        job_result = _load_live(fixture_name)
        tier1 = classify_tier1(job_result)
        assert tier1.verdict == "bid"
        assert tier1.confidence == "strong"


# ── 场景 C：prj2/prj1 混合 —— design/24 的原始驱动场景，design/27 步骤5的
#    验收场景（华泾镇 D5B-1 电缆项目）───────────────────────────────────────

class TestScenarioC_MixedByNecessity:
    """招标 PDF 没有嵌入清单（Tier 0 文字层仍可判），Excel 附件是定义性的
    采购清单（0 价格列）。投标侧四份 PDF 的 Tier 1 判定已经在真实回归里
    验证过（design/27 步骤5，commit 8092525），这里不重新构造识别产物
    去重复断言——那需要编造 job_result 内容，宁可显式跳过并注明去处。"""

    def test_tender_pdf_tier0_native_text_layer(self):
        path = DOCS / "tender/prj2_电缆招标.pdf"
        _skip_if_missing(path)
        result = classify_pdf(str(path))
        assert result.text_layer == "native"

    def test_tender_list_excel_is_definitive_no_price_columns(self):
        """§4 原文这份文件"no embedded list, so an Excel supplement is
        required"——独立验证它本身是无价格列的定义性采购清单。"""
        path = DOCS / "tender_list/prj2_附件一_电缆清单.xlsx"
        _skip_if_missing(path)
        result = classify_excel(str(path))
        assert result.verdict == "tender_list"
        assert result.confidence == "definitive"
        assert result.price_columns == []

    @pytest.mark.parametrize("pdf_name", [
        "prj1_上海浦东.pdf", "prj1_亨通.pdf", "prj1_宏胜.pdf", "prj1_远东.pdf",
    ])
    def test_bid_pdf_tier0_scanned(self, pdf_name):
        """Tier 0（文字层）用真实文件验证；Tier 1（招标/投标判定）在真实
        回归里已经验证过，此处不重复。"""
        path = DOCS / "bid" / pdf_name
        _skip_if_missing(path)
        result = classify_pdf(str(path))
        assert result.text_layer == "scanned"

    def test_bid_pdf_tier1_covered_by_live_regression_not_here(self):
        """记录缺口而不是假装覆盖：见本文件顶部 docstring 与
        design/27 步骤5 commit 8092525（真实 4 份投标 PDF 识别 263/132/
        132/139 行，checksum_ack / missing_total_requires_review 两类
        门禁行为均验证过）。"""
        pytest.skip(
            "prj1 四份投标 PDF 的识别产物未落成夹具文件——Tier 1 判定已在"
            "design/27 步骤5的真实端到端回归中验证（commit 8092525），"
            "不在此处用编造数据重新断言。"
        )
