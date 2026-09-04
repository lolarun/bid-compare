"""design/26 P4 —— ExtractionPipeline.extract_quote 的阶段内进度转发（Paddle 路径）。

design/24 B2 原本假设 Paddle 能报"已转录 N 行"这类逐行/逐页粒度的进度（跟 qwen
流式一样），P4 接线时核实过 Baidu 的轮询响应只有一个状态字段
（running/success/failed），没有逐页细分——那个假设是错的，改成验证
`recognize_quote_paddle` 实际会报的三段粗粒度进度确实被转发到外层
progress_cb，不是接了参数没接线。阶段命名跟 design/27 §6 对齐（"识别内容/
提取信息/整理完成"，不带引擎术语）。
"""
from __future__ import annotations

from apps.api.intelligence.base import LLMProvider
from apps.api.intelligence.pipeline import ExtractionPipeline

HEAD = ("row_type,seq,name,spec,unit,qty,brand,remark,unit_price_excl_tax,"
       "total_price_excl_tax,tax_rate,tax_amount,unit_price_incl_tax,"
       "total_price_incl_tax,unit_price,total_price,copy_no,page")


class _UnusedTenderOnlyProvider(LLMProvider):
    """占位 provider——报价识别不再读它（design/26 P4：quote 走 Paddle，不经过
    `LLMProvider` 抽象），这里只是满足 `ExtractionPipeline.__init__` 的构造要求
    （招标侧仍然用它）。**不能用 `MockProvider`**：`extract_quote` 显式识别
    `MockProvider` 作为测试替身、直接走它的 `vl_extract_csv`（服务另外 35 个
    只关心下游入库/对齐逻辑的集成测试），会绕过本测试真正要验证的 Paddle
    进度转发路径。"""
    name = "unused-tender-only"

    def extract(self, images, schema, prompt, timeout=90):
        raise NotImplementedError  # pragma: no cover — 本测试不会调用招标识别

    def vl_extract_csv(self, images, prompt, *, model=None, labels=None, **kwargs):
        raise NotImplementedError  # pragma: no cover — 本测试不会调用招标识别


def test_extract_quote_forwards_paddle_stage_progress_to_outer_callback(monkeypatch, tmp_path):
    from apps.api.intelligence.document_loader import DocumentLoader
    from apps.api.intelligence.providers import paddle_ocr

    monkeypatch.setattr(DocumentLoader, "get_page_count", staticmethod(lambda _p: 1))

    fake_doc_json = {"pages": [{"page_num": 0, "tables": []}]}

    def fake_submit_and_parse(file_path, **_kw):
        return fake_doc_json

    monkeypatch.setattr(paddle_ocr, "submit_and_parse", fake_submit_and_parse)

    calls = []

    def notify(stage, pct, *, stage_current=None, stage_total=None):
        calls.append((stage, pct, stage_current, stage_total))

    pipeline = ExtractionPipeline(_UnusedTenderOnlyProvider())  # 招标侧占位，报价走 Paddle
    resp = pipeline.extract_quote("x.pdf", {}, progress_cb=notify)

    # 没有可辨认报价表（fake_doc_json 里 tables 为空）时 build_quote_csv 返回
    # None，build_draft 拿空壳 CSV 走 BLOCKED——这里只关心进度转发，不关心
    # 识别结果本身，主流程不能因为进度回调而崩。
    assert resp.data is not None

    stages = [c[0] for c in calls]
    assert "识别内容" in stages, f"识别阶段进度未转发，收到={calls}"
    assert "整理完成" in stages, f"整理阶段进度未转发，收到={calls}"

    # 识别内容阶段的首次通知要带 stage_current/stage_total（design/27 §6：
    # 没有页数就没有预计耗时，但已耗时=0 这个起点必须报出去，不能整段哑火）。
    first_recognize = next(c for c in calls if c[0] == "识别内容")
    assert first_recognize[2] == 0, f"识别内容阶段首次通知应带 stage_current=0，收到={first_recognize}"
