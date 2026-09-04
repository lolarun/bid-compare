"""design/26 P1 二次复核：`scripts/e2e_diff.py` 分块内容对齐的回归测试。

放在 apps/api/tests/（不是 scripts/）——虽然被测模块 e2e_diff.py 本身在
scripts/ 下，但 scripts/test_*.py 这个前缀在本仓库已经被用作"需要跑起来的
后端、真实网络调用的手工 E2E 脚本"（如 scripts/test_e2e_alignment.py），
跟这里"零网络、零依赖的 pytest 单元测试"是两回事，放同一目录会跟现有
命名撞车、也不会被 pyproject.toml 的 testpaths（只含 apps/api/tests 和
tests）自动收集到——本文件就近放进已被收集的目录，只是通过 sys.path 引用
scripts/ 下的被测模块。

这几个函数现在是 P2a 全部准确率结论的承重墙（亨通 65.4%→97.8%、宏胜
100%→28.7%→94.8%→98.5% 三次反转都靠它），此前零测试——不符合本仓库的一贯
要求（`.claude/rules/tests.md`：单元测试验证局部契约），补上。

不依赖网络、不依赖 outputs/（gitignore，非受控产物）——全部用手搭的小型
DraftRow/golden-row 结构。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from e2e_diff import (  # noqa: E402
    _content_match_blocked,
    _match_blocks,
    _name_sim,
    _split_blocks_by_name,
)


def _q(name: str, **fields) -> SimpleNamespace:
    """造一个 quote_lines 元素——`_row_name`/`_content_match` 只要 `.fields`。"""
    return SimpleNamespace(fields={"name": name, **fields})


def _g(name: str, **fields) -> dict:
    return {"name": name, **fields}


# ─── §1 按 name 切块 ─────────────────────────────────────────────────────────

def test_split_blocks_groups_consecutive_same_name():
    items = [_q("A"), _q("A"), _q("B"), _q("B"), _q("B")]
    assert _split_blocks_by_name(items) == [(0, 1), (2, 4)]


def test_split_blocks_merges_substring_fragments():
    # "普通" 是 "普通电缆" 的子串——同一类目名被拆成两种写法（Paddle 跨行
    # 换行实测复现），不该切成两个块。
    items = [_q("普通电缆"), _q("普通"), _q("普通电缆")]
    assert _split_blocks_by_name(items) == [(0, 2)]


def test_split_blocks_does_not_merge_unrelated_names():
    # "矿物电缆"/"普通电缆" 共享"电缆"后缀但不是子串关系——不能被合并
    # （子串判据故意比模糊阈值精确，见模块内注释）。
    items = [_q("矿物电缆"), _q("普通电缆")]
    assert _split_blocks_by_name(items) == [(0, 0), (1, 1)]


# ─── §2 块级配对 + 就近合并孤儿块 ────────────────────────────────────────────

def test_match_blocks_pairs_by_name_similarity():
    quote = [_q("矿物电缆"), _q("普通电缆")]
    golden = [_g("普通电缆"), _g("矿物电缆")]  # 顺序颠倒
    q_blocks = [(0, 0), (1, 1)]
    g_blocks = [(0, 0), (1, 1)]
    pairs = _match_blocks(q_blocks, quote, g_blocks, golden)
    # golden[0]=普通电缆 应配上 quote 的 (1,1)（普通电缆）；golden[1]=矿物电缆 配 (0,0)。
    assert pairs[0] == ((1, 1), (0, 0))
    assert pairs[1] == ((0, 0), (1, 1))


def test_match_blocks_merges_orphan_block_into_preceding_anchor():
    # 宏胜实测形状：报价侧比 golden 侧碎（"预分支电缆头"被拆出独立小段），
    # 碎块应该并入前一个锚点块，不能整块丢弃（旧版行为：recall 100%→28.7%）。
    quote = [_q("矿物电缆"), _q("预分支电缆头"), _q("普通电缆")]
    golden = [_g("矿物电缆"), _g("普通电缆")]
    q_blocks = [(0, 0), (1, 1), (2, 2)]
    g_blocks = [(0, 0), (1, 1)]
    pairs = _match_blocks(q_blocks, quote, g_blocks, golden)
    assert pairs[0] == ((0, 1), (0, 0))   # 矿物电缆锚点吸收了紧跟着的孤儿块
    assert pairs[1] == ((2, 2), (1, 1))


def test_match_blocks_global_best_score_wins_over_first_seen():
    # 真实 bug 复现（本轮实测捕获）：按报价块出现顺序逐个贪心，会让排在
    # 前面、只是弱相似的块（"预分支电缆头"跟"普通电缆"沾点边）抢先认领掉
    # 一个 golden 块，等真正高分的块（"普通电缆" 对 "普通电缆"，score=1.0）
    # 出场时已经没有块可配——必须全局按分数降序认领，不能按出现顺序贪心。
    quote = [_q("矿物电缆"), _q("预分支电缆头"), _q("普通电缆")]
    golden = [_g("矿物电缆"), _g("普通电缆")]
    q_blocks = [(0, 0), (1, 1), (2, 2)]
    g_blocks = [(0, 0), (1, 1)]
    pairs = _match_blocks(q_blocks, quote, g_blocks, golden)
    # 普通电缆(golden)必须配上普通电缆(报价 q_block2)，不能被"预分支电缆头"抢走。
    assert pairs[1][0] is not None and pairs[1][0][1] == 2


def test_match_blocks_merges_leading_orphan_into_following_anchor():
    # 孤儿块出现在**第一个**锚点之前——没有"前一个"可以并入，回填给后面
    # 第一个锚点。
    quote = [_q("招标文件条目号"), _q("矿物电缆")]
    golden = [_g("矿物电缆")]
    q_blocks = [(0, 0), (1, 1)]
    g_blocks = [(0, 0)]
    pairs = _match_blocks(q_blocks, quote, g_blocks, golden)
    assert pairs[0] == ((0, 1), (0, 0))


def test_match_blocks_unmatched_golden_block_stays_none():
    quote = [_q("矿物电缆")]
    golden = [_g("矿物电缆"), _g("普通电缆")]
    q_blocks = [(0, 0)]
    g_blocks = [(0, 0), (1, 1)]
    pairs = _match_blocks(q_blocks, quote, g_blocks, golden)
    matched_gis = {i for i, (qb, gb) in enumerate(pairs) if qb is not None}
    assert len(matched_gis) == 1  # 只有一个 golden 块配上了报价块


# ─── §3 端到端：分块对齐 vs 直接退回原算法 ──────────────────────────────────

def test_content_match_blocked_recovers_reordered_sections():
    # 亨通实测的最小复现：两个类目段整段颠倒，块级对齐应该都能匹配上，
    # 不像原始 _content_match（单条 DP，要求下标同步递增）那样只能对上一段。
    quote = ([_q("普通电缆", qty=i) for i in range(20, 24)]
            + [_q("矿物电缆", qty=i) for i in range(1, 5)])
    golden = ([_g("矿物电缆", qty=i) for i in range(1, 5)]
             + [_g("普通电缆", qty=i) for i in range(20, 24)])
    gi_to_draft, gi_to_score = _content_match_blocked(quote, golden)
    assert len(gi_to_draft) == 8  # 8 个 golden 行全部匹配上


def test_content_match_blocked_falls_back_when_block_ratio_too_skewed():
    # 报价侧块数远多于 golden 侧（>3倍）——不可信的分块信号，退回整份 DP。
    # 这里构造一个块数比例失衡但内容其实顺序一致的输入：退回原算法后应该
    # 能按行序对齐，不会因为分块噪声丢数据。
    quote = [_q(f"item{i}", qty=i) for i in range(12)]  # 12 个各自独立的"块"
    golden = [_g(f"item{i}", qty=i) for i in range(12)]
    gi_to_draft, gi_to_score = _content_match_blocked(quote, golden)
    assert len(gi_to_draft) == 12  # 退回整份 DP 后顺序对齐，全部匹配


def test_content_match_blocked_single_block_document_uses_plain_dp():
    # 只有一个类目（不需要分块）——直接走原算法，不强行分块。
    quote = [_q("阀门", qty=i) for i in range(5)]
    golden = [_g("阀门", qty=i) for i in range(5)]
    gi_to_draft, gi_to_score = _content_match_blocked(quote, golden)
    assert len(gi_to_draft) == 5


# ─── §4 name 相似度（子函数，供 §2/§3 复用）──────────────────────────────────

def test_name_sim_identical_is_one():
    assert _name_sim("矿物电缆", "矿物电缆") == 1.0


def test_name_sim_unrelated_is_low():
    assert _name_sim("矿物电缆", "阀门") < 0.3
