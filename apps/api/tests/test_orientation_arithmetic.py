"""四向方向纠正的算术判据单元测试（doc/19 §L1、§L2）—— 无 OCR/LLM/API。

中心假设：列覆盖度对 180° 天然失明（翻转保留列名集合、只打乱取值），
必须用 qty × 单价 ≈ 合价 才能把倒置/列错位的页分离出来。
夹具取自 tmp/api_e2e_cable 的真实失败样本（宏胜矿物电缆）。
"""
from __future__ import annotations

import pytest

from apps.api.intelligence.table_recognizer import (
    _grid_arithmetic, _orientation_score, _orient_candidates, _score_key,
    _orientation_quality, _ORIENT_MIN_GOOD, _ARITH_MIN_OK, _ARITH_MIN_ROWS,
)

# 正立：数量 × 单价 = 合价 全部成立（数字取自远东 p4 转正后的真实 OCR 输出）
UPRIGHT_HTML = """<table>
  <tr><td>序号</td><td>名称</td><td>规格型号</td><td>单位</td><td>数量</td>
      <td>单价</td><td>合价</td></tr>
  <tr><td>15</td><td>矿物电缆</td><td>RTTYZ-6*150+E70</td><td>米</td>
      <td>148.54</td><td>934.47</td><td>138806.18</td></tr>
  <tr><td>16</td><td>矿物电缆</td><td>RTTYZ-3*95+2*50</td><td>米</td>
      <td>779.78</td><td>375.35</td><td>292690.43</td></tr>
  <tr><td>17</td><td>矿物电缆</td><td>RTTYZ-3*50+E25</td><td>米</td>
      <td>176.13</td><td>169.78</td><td>29903.36</td></tr>
  <tr><td>18</td><td>矿物电缆</td><td>RTTYZ-4*35+E16</td><td>米</td>
      <td>7.00</td><td>151.32</td><td>1059.24</td></tr>
</table>"""

# 180° 倒置后列被打乱：列名集合完全相同，但取值移了一列 —— 这正是宏胜的真实症状
# （合价 1987156.70 被读成 qty 1987.1567，数量落到单价、单价落到合价）。
SCRAMBLED_HTML = """<table>
  <tr><td>序号</td><td>名称</td><td>规格型号</td><td>单位</td><td>数量</td>
      <td>单价</td><td>合价</td></tr>
  <tr><td>1</td><td>矿物电缆</td><td>RTTYZ-3*240+2*120</td><td>米</td>
      <td>1987.1567</td><td>1905.25</td><td>1042.99</td></tr>
  <tr><td>2</td><td>矿物电缆</td><td>RTTYZ-3*185+2*95</td><td>米</td>
      <td>2882.94</td><td>2882.94</td><td>839.25</td></tr>
  <tr><td>3</td><td>矿物电缆</td><td>RTTYZ-4*150+2*70</td><td>米</td>
      <td>2987.24</td><td>2987.24</td><td>562.51</td></tr>
  <tr><td>4</td><td>矿物电缆</td><td>RTTYZ-4*50+E70</td><td>米</td>
      <td>207.52</td><td>207.52</td><td>534.43</td></tr>
</table>"""

# 轴不对（侧向）：表头几乎认不出来
SIDEWAYS_HTML = """<table>
  <tr><td>矿物电缆</td><td>米</td></tr>
  <tr><td>数量 148.54</td><td>规格</td></tr>
</table>"""


class TestArithmeticSeparates180:
    """覆盖度分不出来的，算术必须分得出来。"""

    def test_coverage_alone_cannot_tell_them_apart(self):
        up = _orientation_quality(UPRIGHT_HTML, 1, "quote")
        sc = _orientation_quality(SCRAMBLED_HTML, 1, "quote")
        assert up >= _ORIENT_MIN_GOOD
        assert sc == up, "列覆盖度对 180° 失明——这正是必须引入算术判据的原因"

    def test_arithmetic_does_tell_them_apart(self):
        up_rows, up_ratio = _grid_arithmetic(UPRIGHT_HTML, 1)
        sc_rows, sc_ratio = _grid_arithmetic(SCRAMBLED_HTML, 1)
        assert up_rows >= _ARITH_MIN_ROWS and up_ratio == 1.0
        assert sc_rows >= _ARITH_MIN_ROWS and sc_ratio < _ARITH_MIN_OK

    def test_score_key_orders_upright_above_scrambled(self):
        up = _score_key(_orientation_score(UPRIGHT_HTML, 1, "quote"))
        sc = _score_key(_orientation_score(SCRAMBLED_HTML, 1, "quote"))
        assert up > sc


class TestCandidateSelection:
    """懒探：0° 的表现决定探哪些角度，正立文档一次都不探。"""

    def test_upright_probes_nothing(self):
        score = _orientation_score(UPRIGHT_HTML, 1, "quote")
        assert _orient_candidates(score, "quote") == ()

    def test_bad_coverage_probes_both_axes(self):
        score = _orientation_score(SIDEWAYS_HTML, 1, "quote")
        assert _orient_candidates(score, "quote") == (90, 270)

    def test_good_coverage_bad_arithmetic_probes_180_only(self):
        score = _orientation_score(SCRAMBLED_HTML, 1, "quote")
        assert _orient_candidates(score, "quote") == (180,)


class TestNoEvidenceIsNotFailure:
    """判定不了不等于方向错——不得因为行太少就把页判成翻转。"""

    def test_too_few_rows_reports_no_evidence(self):
        one_row = """<table>
          <tr><td>名称</td><td>规格</td><td>单位</td><td>数量</td><td>单价</td><td>合价</td></tr>
          <tr><td>电缆</td><td>YJV-4*70</td><td>米</td><td>10</td><td>5</td><td>999</td></tr>
        </table>"""
        rows, ratio = _grid_arithmetic(one_row, 1)
        assert rows < _ARITH_MIN_ROWS and ratio == 1.0
        assert _orient_candidates(_orientation_score(one_row, 1, "quote"), "quote") == ()

    def test_tender_docs_keep_coverage_only_scoring(self):
        """招标清单没有价格列，算术判据不适用，不得因此触发 180° 探测。"""
        score = _orientation_score(SCRAMBLED_HTML, 1, "tender")
        assert score[1] == 0 and score[2] == 1.0
        assert 180 not in _orient_candidates(score, "tender")


@pytest.fixture(autouse=True)
def _force_orient_v2(monkeypatch):
    """本文件验证的是 doc/19 §L1 的 V2 方向逻辑；它默认关闭，需显式打开。"""
    import apps.api.intelligence.table_recognizer as tr
    monkeypatch.setattr(tr, "_ORIENT_V2", True)
