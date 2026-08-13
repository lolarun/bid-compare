"""design/24 B2 —— ExtractionPipeline.extract_quote 的阶段内进度转发。

vl_quote.py / dashscope_ocr.py 两侧的逻辑已经在 test_vl_quote.py /
test_vl_streaming.py 里分别测过；这里补上中间那一环——pipeline.py 的
`_vl_call` 包装器必须真的把 provider 的 row_progress_cb 转发到外层
progress_cb 上，不是接了参数没接线。
"""
from __future__ import annotations

from apps.api.intelligence.base import LLMProvider
from apps.api.intelligence.pipeline import ExtractionPipeline


HEAD = "row_type,材料名称,规格型号,单位,数量,单价,合价,copy_no,page"


class _RowProgressProvider(LLMProvider):
    """假 provider：vl_extract_csv 一收到 row_progress_cb 就立刻回调一次，
    模拟 dashscope_ocr.py 里流式过程中触发进度上报的效果。"""
    name = "fake-row-progress"

    def vl_extract_csv(self, images, prompt, *, model=None, labels=None,
                        row_progress_cb=None, **_kw):
        if labels:
            return "1,0"   # 方向预检分支：不用转
        if row_progress_cb is not None:
            row_progress_cb(42)
        return HEAD + "\ndetail,电缆,A-1,米,1,1,1,1,1"

    def extract(self, images, schema, prompt, **_kw):  # pragma: no cover — 不走这条
        raise NotImplementedError


def test_extract_quote_forwards_row_progress_to_outer_callback(monkeypatch, tmp_path):
    from PIL import Image
    import io as _io
    import apps.api.intelligence.vl_quote as vd

    def fake_render(_path, pages):
        buf = _io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
        b = buf.getvalue()
        return {p: b for p in pages}

    class _L:
        @staticmethod
        def get_page_count(_p):
            return 1
        render_pages = staticmethod(fake_render)

    monkeypatch.setattr(vd, "DocumentLoader", _L)

    calls = []

    def notify(stage, pct, *, stage_current=None, stage_total=None):
        calls.append((stage, pct, stage_current, stage_total))

    pipeline = ExtractionPipeline(_RowProgressProvider())
    resp = pipeline.extract_quote("x.pdf", {}, progress_cb=notify)

    assert resp.data["items"], "识别本身必须正常产出，进度转发不能干扰主流程"
    # 外层 progress_cb 应该收到一条 (识别报价清单, 55, 42, None)——
    # 55 是 vl_quote.py 里这个阶段固定的起始 pct，stage_total=None 如实说
    # "没有总数"，不是留空。
    assert ("识别报价清单", 55, 42, None) in calls
