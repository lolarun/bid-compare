"""页面方向纠正单元测试（item 4）—— 无 OCR/LLM/API。

覆盖：正常、90°、270°、混合方向、单页旋转、方向并列、probe失败、无价格采购清单。

设计：FakeProvider 不打真实 API，按「图像尺寸」决定返回哪段 HTML——
原图 W×H，旋转 90/270 后变 H×W，据此映射不同方向下 OCR 应得的 HTML，
从而确定性地驱动 _detect_chain_orientation / _correct_page_orientation。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from apps.api.intelligence.table_recognizer import (
    _orientation_quality, _detect_chain_orientation, _correct_page_orientation,
    _rotate_png_bytes, _contiguous_runs, _ORIENT_MIN_GOOD,
)

# ── HTML 夹具：高/低方向质量 ────────────────────────────────────────────────
# 高质量报价表头：覆盖 name/spec/unit/qty/price 多个核心列 → q 高
GOOD_QUOTE_HTML = """<table>
  <tr><td>序号</td><td>名称</td><td>规格</td><td>单位</td><td>数量</td>
      <td>单价</td><td>价税合计</td></tr>
  <tr><td>1</td><td>闸阀</td><td>DN50</td><td>个</td><td>2</td><td>100</td><td>226</td></tr>
</table>"""
# 低质量（错位/侧向）：表头仅 1 个可识别列(名称) → 0<q<MIN_GOOD（suspect 特征）。
# 数据行含「数量」字样以触发 _orientation_signal（模拟侧向页仍有价量关键词残留）。
BAD_QUOTE_HTML = """<table>
  <tr><td>名称</td><td>aa</td><td>bb</td></tr>
  <tr><td>闸阀</td><td>个</td><td>数量1</td></tr>
</table>"""
# 无 grids（无表头/无边框）→ q=0
NOGRID_HTML = "<table><tr><td>说明文字一行</td></tr></table>"
# 采购清单（无价格）高质量：name/spec/unit/qty
GOOD_TENDER_HTML = """<table>
  <tr><td>序号</td><td>项目名称</td><td>规格</td><td>单位</td><td>数量</td></tr>
  <tr><td>1</td><td>闸阀</td><td>DN50</td><td>个</td><td>2</td></tr>
</table>"""


def _png(w: int = 800, h: int = 600) -> bytes:
    """白底图，左上角 12×12 红块作方向标记。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(12):
        for y in range(12):
            im.putpixel((x, y), (255, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _detected_rotation(image: bytes) -> int:
    """读红块所在角，推回该图相对原图被旋转的角度（0/90/270）。

    _rotate_png_bytes(img,90)=rotate(-90,expand)=顺时针90° → 左上→右上。
    _rotate_png_bytes(img,270)=rotate(-270)=逆时针90° → 左上→左下。
    """
    with Image.open(io.BytesIO(image)) as im:
        w, h = im.size
        px = im.load()
        def red(x, y):
            r, g, b = px[x, y][:3]
            return r > 200 and g < 80 and b < 80
        if red(0, 0):
            return 0
        if red(w - 1, 0):
            return 90
        if red(0, h - 1):
            return 270
        return 180


class FakeProvider:
    """按「检测到的旋转角」映射 HTML。rot_to_html: {0/90/270: html}。"""

    def __init__(self, rot_to_html: dict, fail_on: set | None = None):
        self.rot_to_html = rot_to_html
        self.fail_on = fail_on or set()
        self.calls: list[int] = []

    def ocr_pages_with_roles(self, images):
        results = []
        for img in images:
            rot = _detected_rotation(img)
            self.calls.append(rot)
            if rot in self.fail_on:
                raise RuntimeError("simulated OCR failure")
            html = self.rot_to_html.get(rot, NOGRID_HTML)
            results.append(("quote_table", html))
        return results, []


# ── _orientation_quality ───────────────────────────────────────────────────

def test_quality_good_quote_high():
    assert _orientation_quality(GOOD_QUOTE_HTML, 1, "quote") >= _ORIENT_MIN_GOOD


def test_quality_bad_quote_low():
    assert _orientation_quality(BAD_QUOTE_HTML, 1, "quote") < _ORIENT_MIN_GOOD


def test_quality_nogrid_zero():
    assert _orientation_quality(NOGRID_HTML, 1, "quote") == 0


def test_quality_tender_signal_split():
    # 采购清单无价格列，但 name/spec/unit/qty 齐 → 高质量
    assert _orientation_quality(GOOD_TENDER_HTML, 1, "tender") >= _ORIENT_MIN_GOOD


# ── 正常（upright）：无候选、无旋转 ─────────────────────────────────────────

def test_normal_no_rotation():
    prov = FakeProvider({0: GOOD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: GOOD_QUOTE_HTML})
    htmls = [GOOD_QUOTE_HTML, GOOD_QUOTE_HTML]
    imgs = [_png(), _png()]
    angle, _probe = _detect_chain_orientation([1, 2], htmls, imgs, prov, "quote")
    assert angle == 0
    # 已正立：不应触发任何 OCR 探测（成本/回放确定性）
    assert prov.calls == []


# ── 90° 旋转：原图低质量，转 90 后高质量 ────────────────────────────────────

def test_rotate_90_detected_and_applied():
    # 原图(0°)=BAD；转90=GOOD；转270=BAD → 仅 90 改善
    prov = FakeProvider({0: BAD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: BAD_QUOTE_HTML})
    htmls = [BAD_QUOTE_HTML]
    imgs = [_png()]
    angle, _probe = _detect_chain_orientation([1], htmls, imgs, prov, "quote")
    assert angle == 90
    h2, img2, deg = _correct_page_orientation(htmls[0], imgs[0], 1, prov, "quote", {angle})
    assert deg == 90
    assert h2 == GOOD_QUOTE_HTML


# ── 270° 旋转 ───────────────────────────────────────────────────────────────

def test_rotate_270_detected_and_applied():
    prov = FakeProvider({0: BAD_QUOTE_HTML, 90: BAD_QUOTE_HTML, 270: GOOD_QUOTE_HTML})
    htmls = [BAD_QUOTE_HTML]
    imgs = [_png()]
    angle, _probe = _detect_chain_orientation([1], htmls, imgs, prov, "quote")
    assert angle == 270
    h2, img2, deg = _correct_page_orientation(htmls[0], imgs[0], 1, prov, "quote", {angle})
    assert deg == 270
    assert h2 == GOOD_QUOTE_HTML


# ── 方向并列（90 与 270 同样高）→ 不旋转 ────────────────────────────────────

def test_tie_no_rotation():
    # 90 与 270 列覆盖求和并列更高 → 方向不可判（90≈270）→ 返回 0，不旋转
    prov = FakeProvider({0: BAD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: GOOD_QUOTE_HTML})
    htmls = [BAD_QUOTE_HTML]
    imgs = [_png()]
    angle, _probe = _detect_chain_orientation([1], htmls, imgs, prov, "quote")
    assert angle == 0


# ── 单页旋转：文档里只有一页旋转，其余已正 ─────────────────────────────────

def test_single_page_rotation_mixed_doc():
    prov = FakeProvider({0: GOOD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: BAD_QUOTE_HTML})
    # page1 原图已正(GOOD)：q0 高，转 90/270 不更高 → 不动
    h1, _i1, d1 = _correct_page_orientation(GOOD_QUOTE_HTML, _png(), 1, prov, "quote", {90})
    assert d1 == 0 and h1 == GOOD_QUOTE_HTML
    # page2 原图侧向(BAD)：转 90 高 → 转
    h2, _i2, d2 = _correct_page_orientation(BAD_QUOTE_HTML, _png(), 2, prov, "quote", {90})
    assert d2 == 90 and h2 == GOOD_QUOTE_HTML


# ── 无表头续表页：q0=0 且旋转后仍 0，单候选 → 信任文档信号转正 ──────────────

def test_headerless_continuation_uses_single_candidate():
    prov = FakeProvider({0: NOGRID_HTML, 90: NOGRID_HTML, 270: NOGRID_HTML})
    h2, img2, deg = _correct_page_orientation(NOGRID_HTML, _png(), 5, prov, "quote", {90})
    assert deg == 90  # 信任文档单候选


def test_headerless_not_rotated_when_multiple_candidates():
    prov = FakeProvider({0: NOGRID_HTML, 90: NOGRID_HTML, 270: NOGRID_HTML})
    # 混合方向(>1候选) + 测不出 → 不旋转
    h2, img2, deg = _correct_page_orientation(NOGRID_HTML, _png(), 5, prov, "quote", {90, 270})
    assert deg == 0


# ── probe 失败：优雅降级，不崩溃 ────────────────────────────────────────────

def test_probe_failure_graceful():
    # 所有旋转探测抛异常 → 无候选，不崩溃
    prov = FakeProvider({0: BAD_QUOTE_HTML}, fail_on={90, 270})
    htmls = [BAD_QUOTE_HTML]
    imgs = [_png()]
    angle, _probe = _detect_chain_orientation([1], htmls, imgs, prov, "quote")
    assert angle == 0
    h2, img2, deg = _correct_page_orientation(htmls[0], imgs[0], 1, prov, "quote", {90})
    assert deg == 0  # 失败页保留原图


# ── 无价格采购清单：tender 信号检测，不依赖价格关键词 ───────────────────────

def test_tender_no_price_detection():
    bad_tender = ("<table><tr><td>名称</td><td>aa</td><td>bb</td></tr>"
                  "<tr><td>闸阀</td><td>个</td><td>数量2</td></tr></table>")
    prov = FakeProvider({0: bad_tender, 90: GOOD_TENDER_HTML, 270: bad_tender})
    htmls = [bad_tender]
    imgs = [_png()]
    angle, _probe = _detect_chain_orientation([1], htmls, imgs, prov, "tender")
    assert angle == 90


# ── _contiguous_runs ───────────────────────────────────────────────────────

def test_contiguous_runs_splits_on_gaps():
    assert _contiguous_runs([4, 5, 6, 9, 10]) == [[4, 5, 6], [9, 10]]
    assert _contiguous_runs([3]) == [[3]]
    assert _contiguous_runs([]) == []
    assert _contiguous_runs([7, 5, 6]) == [[5, 6, 7]]   # 排序后再切段


# ── 连续表链：续页（q=0）继承表头页方向（泰科龙 p5-p14 形态） ─────────────────

def test_chain_continuation_inherits_orientation():
    """链首页侧向(BAD,转90=GOOD)，续页 q=0(NOGRID,各方向都测不出)。

    链级判向只看锚点页（有方向信号者）→ 判 90；续页经单候选继承转正。
    """
    prov = FakeProvider({0: BAD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: BAD_QUOTE_HTML})
    # p1 锚点（BAD，有数量信号），p2 续页（NOGRID，无方向信号、q=0）
    htmls = [BAD_QUOTE_HTML, NOGRID_HTML]
    imgs = [_png(), _png()]
    angle, _probe = _detect_chain_orientation([1, 2], htmls, imgs, prov, "quote")
    assert angle == 90, "锚点页判向应代表整条链"
    # 续页 p2 用链方向单候选继承（q0=0 且单候选 → 信任转正）
    _h, _i, deg = _correct_page_orientation(htmls[1], imgs[1], 2, prov, "quote", {angle})
    assert deg == 90, "续页应继承链方向转正"


def test_chain_all_good_no_probe():
    """整条链已正立（kaishuo/miancun 形态）→ 返回 0，零 OCR 探测（回放确定性保护）。"""
    prov = FakeProvider({0: GOOD_QUOTE_HTML, 90: GOOD_QUOTE_HTML, 270: GOOD_QUOTE_HTML})
    htmls = [GOOD_QUOTE_HTML] * 7   # page_htmls 按 1-based 页号索引，需覆盖 p1..p7
    imgs = [_png() for _ in range(7)]
    angle, _probe = _detect_chain_orientation([3, 4, 5, 6, 7], htmls, imgs, prov, "quote")
    assert angle == 0
    assert prov.calls == [], "已正立链不得触发旋转探测"
