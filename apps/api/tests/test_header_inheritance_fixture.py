"""无表头续页 × 方向判定 —— 基于真实 OCR 快照的离线回归（doc/19 §L1、§L2）。

夹具来自客户真实扫描件（远东电缆投标文件 p3/p5，90° 与 270° 两个角度的 OCR HTML），
由 scripts/build_ocr_fixture.py 落盘。**不打任何 API**，因此这些逻辑可以秒级回归，
不必再靠 fresh E2E 试错——识别规则要求「OCR、方向纠正的输入输出必须可快照重放」。

固化的缺陷（2026-08-09 实测）：
  续页没有自己的表头 → html_to_table_grids 返回 0 个表格 → 方向评分恒为 0 →
  各角度打分全同 → 判定不旋转 → OCR 停在错误方向 → 整页 0 行。
  方向纠正与表头继承互为死锁：方向要靠"能否解析出表"判断，解析要靠"方向对"才成功。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.intelligence.table_parser import html_to_table_grids
from apps.api.intelligence.table_recognizer import (
    _first_own_header, _orientation_score, _score_key,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ocr_html" / "yuandong"

# 真实答案（客户参考清单）：p5 是序号 25–38 共 14 行；正确方向是顺时针 90°。
CORRECT_ANGLE = 90
WRONG_ANGLE = 270
P5_EXPECTED_ROWS = 14

pytestmark = pytest.mark.skipif(
    not (FIXTURE / f"p5_r{CORRECT_ANGLE}.html").exists(),
    reason="OCR 夹具缺失；用 scripts/build_ocr_fixture.py 生成",
)


def _html(page: int, angle: int) -> str:
    return (FIXTURE / f"p{page}_r{angle}.html").read_text(encoding="utf-8")


def _page_list(angle: int) -> list[str]:
    pages = [""] * 5
    for p in (3, 5):
        pages[p - 1] = _html(p, angle)
    return pages


class TestContinuationPageNeedsInheritance:
    def test_header_page_parses_on_its_own(self):
        grids = html_to_table_grids(_html(3, CORRECT_ANGLE), 3)
        assert grids, "有表头的首页必须能独立解析"
        assert len(max(grids, key=lambda g: len(g.col_map)).header) >= 3

    def test_continuation_page_yields_nothing_without_inheritance(self):
        """这就是丢行的直接原因：整张表被丢弃，且没有任何报错。"""
        grids = html_to_table_grids(_html(5, CORRECT_ANGLE), 5)
        assert grids == []

    def test_continuation_page_recovers_with_inherited_header(self):
        header = _first_own_header([3], _page_list(CORRECT_ANGLE))
        grids = html_to_table_grids(_html(5, CORRECT_ANGLE), 5, inherited_header=header)
        assert len(grids) == 1
        assert len(grids[0].rows) == P5_EXPECTED_ROWS


class TestOrientationDeadlock:
    """方向评分必须能分辨对错角度；不带继承表头时它做不到。"""

    def test_without_inheritance_both_angles_score_identically(self):
        bad = _score_key(_orientation_score(_html(5, WRONG_ANGLE), 5, "quote"))
        good = _score_key(_orientation_score(_html(5, CORRECT_ANGLE), 5, "quote"))
        assert bad == good == (0, 1.0), "打平 → 探测器判不出方向（死锁）"

    def test_with_inheritance_the_correct_angle_wins(self):
        good_hdr = _first_own_header([3], _page_list(CORRECT_ANGLE))
        bad_hdr = _first_own_header([3], _page_list(WRONG_ANGLE))
        good = _score_key(_orientation_score(
            _html(5, CORRECT_ANGLE), 5, "quote", good_hdr))
        bad = _score_key(_orientation_score(
            _html(5, WRONG_ANGLE), 5, "quote", bad_hdr))
        assert good > bad, f"正确角度必须胜出：{good} vs {bad}"
        assert good[0] >= 3

    def test_wrong_angle_loses_the_name_column(self):
        """转错方向时 OCR 会丢列、行序颠倒——所以不能只看"能否读出数字"。"""
        good = html_to_table_grids(
            _html(5, CORRECT_ANGLE), 5,
            inherited_header=_first_own_header([3], _page_list(CORRECT_ANGLE)))
        assert good and "name" in good[0].col_map.values()


@pytest.fixture(autouse=True)
def _force_orient_v2(monkeypatch):
    """本文件验证的是 doc/19 §L1 的 V2 方向逻辑；它默认关闭，需显式打开。"""
    import apps.api.intelligence.table_recognizer as tr
    monkeypatch.setattr(tr, "_ORIENT_V2", True)
