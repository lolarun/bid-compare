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


def test_uneven_length_still_detected_via_local_split_search():
    # 长度不能被任何候选 K 整除（9 行，除了 K=3 之外都除不尽）——K=2 靠局部
    # 切点搜索（不是只信名义中点）依然能找到 [a,b,c,d] vs [a,b,c,d,e] 这个
    # 切法：副本 2 多读了一行，这正是"各份副本可能各自多读/漏读一两行"要
    # 覆盖的场景，不该因为整除不了就放弃（design/26 P1 二次复核后收紧了这条：
    # 早期版本在这个用例上判单份，是判据形状问题，不是需要保留的正确行为）。
    block = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    rows = block + block + [("e", 5)]
    assert detect_copies(rows) == [1, 1, 1, 1, 2, 2, 2, 2, 2]




def test_fine_grained_near_duplicate_caught_by_coarse_name_tier():
    # 整行内容（细粒度）相似度只有 6/8=0.75，达不到门槛，但两段的 name 序列
    # （a,b,c,d）逐字相等——粗粒度判据接住了这个信号，两级判据分工正是为此
    # 设计（design/26 P1 二次复核）：细粒度判不出不等于真的没有重复。
    block1 = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    block2 = [("a", 1), ("b", 2), ("c", 3), ("d", "4.01")]  # 最后一行有微小差异
    rows = block1 + block2
    assert detect_copies(rows) == [1, 1, 1, 1, 2, 2, 2, 2]


def test_low_cardinality_category_single_pass_not_treated_as_copy():
    # 粗粒度判据的假阳性护栏：生产真实场景下 name 是低基数类目（"阀门"/"管件"
    # 这种反复出现的大类，不是逐行唯一标签）——一份清单里类目只出现一轮
    # （不是"同一类目段重复了两次"），即使整份只有 2 种类目取值，也不该被
    # 粗粒度判据误判成副本。
    rows = ([("阀门", "DN20")] * 5 + [("阀门", "DN25")] * 5
           + [("管件", "弯头")] * 5 + [("管件", "三通")] * 5)
    assert detect_copies(rows) == [1] * len(rows)


def test_near_identical_blocks_with_minor_ocr_noise_detected_as_copies():
    # 模糊匹配的核心能力：块够长时，个别行的 OCR 噪声不应该拖垮整体判定——
    # 8 行里错 1 行，相似度 14/16=0.875... 仍需验证是否达标，这里用更保守的
    # 16 行/错 1 行（30/32=0.9375）确保稳稳超过 0.90 门槛，同时证明"不要求
    # 逐字节相等"这条能力真的生效（旧版精确匹配算法在这个用例上会判成单份）。
    block1 = [(f"item{i}", f"DN{i}") for i in range(16)]
    block2 = list(block1)
    block2[5] = ("item5", "DN5X")  # 单个字符 OCR 读法差异
    rows = block1 + block2
    result = detect_copies(rows)
    assert result == [1] * 16 + [2] * 16


def test_unequal_length_copies_still_detected():
    # 泰科龙/宏胜实测：各份副本行数不完全相等（独立 OCR，各自可能多读/漏读
    # 一两行）——旧版"精确整除"判据在这种输入上必然判不出来，模糊匹配靠
    # SequenceMatcher 的错位容忍能力应付得了。
    block = [(f"item{i}", i) for i in range(20)]
    copy1 = block
    copy2 = block[:10] + [("item_extra", 999)] + block[10:]  # 副本 2 多读了一行
    rows = copy1 + copy2
    result = detect_copies(rows)
    assert result[:20] == [1] * 20
    assert result[20:] == [2] * 21


def test_two_genuinely_different_tables_not_merged_into_copies():
    # 假阳性护栏：两段内容明显不同的正常清单（不是同一份的重复），即使长度
    # 接近，也不该被判成"这是同一份的两个副本"。
    block1 = [(f"valve{i}", f"DN{i}") for i in range(10)]
    block2 = [(f"cable{i}", f"YJV{i}") for i in range(10)]
    rows = block1 + block2
    assert detect_copies(rows) == [1] * len(rows)
