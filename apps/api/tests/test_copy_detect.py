"""docs/design/26 §5：结构化副本检测（Paddle/qwen 两条识别路径共用）。"""
from __future__ import annotations

from apps.api.intelligence.copy_detect import detect_copies


def test_single_copy_when_too_short_to_judge():
    # 少于两个最小窗口长度——样本太少，宁可判单份，不猜。
    rows = [("a", "1"), ("b", "2"), ("c", "3")]
    assert detect_copies(rows) == [1, 1, 1]


def test_two_identical_blocks_detected_as_two_copies():
    block = [("阀门A", "DN20"), ("阀门B", "DN25"), ("阀门C", "DN32"), ("阀门D", "DN40")]
    rows = block + block
    assert detect_copies(rows) == [1, 1, 1, 1, 2, 2, 2, 2]


def test_three_identical_blocks_detected_as_three_copies():
    block = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    rows = block * 3
    assert detect_copies(rows) == [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3]


def test_no_repetition_stays_single_copy():
    rows = [("阀门A", "DN20"), ("阀门B", "DN25"), ("阀门C", "DN32"), ("阀门D", "DN40"),
            ("阀门E", "DN50"), ("阀门F", "DN65"), ("阀门G", "DN80"), ("阀门H", "DN100")]
    assert detect_copies(rows) == [1] * len(rows)


def test_uneven_length_does_not_force_a_split():
    # 长度不能被任何候选 K 整除（这里是质数）——宁可漏判重复，不可误判正常清单。
    block = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    rows = block + block + [("e", 5)]  # 9 行，9 不能被 2..6 里除 3 之外整除干净且相等
    assert detect_copies(rows) == [1] * len(rows)


def test_near_duplicate_with_one_differing_row_not_treated_as_copy():
    # 宁可漏判：块内容不是逐行严格相等（OCR 噪声导致某一行不同）时不判定重复，
    # 避免把噪声误认成"这是第二份"。
    block1 = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    block2 = [("a", 1), ("b", 2), ("c", 3), ("d", "4.01")]  # 最后一行有微小差异
    rows = block1 + block2
    assert detect_copies(rows) == [1] * len(rows)
