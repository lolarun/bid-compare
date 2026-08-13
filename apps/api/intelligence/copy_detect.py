"""copy_detect.py — 结构化副本检测：同一份清单在文档内重复出现（正本/副本、
汇总/明细）时，按行内容识别第几份，产出 `copy_no`。

design/26 §5：这是 Paddle 与 qwen 两条识别路径共用的基础设施，不是 Paddle 专属。
qwen 路径今天完全依赖 `PROMPT_QUOTE_CSV` 第 3 条提示词——模型要自己数"这是第几份"，
不总照做（提示词是请求，不是保证）。这里给一个不依赖模型自觉性的结构判据：
**整份行序列里，是否存在若干个连续区块，内容彼此高度相似**——那正是"同一张表
被原样重复打印了 K 次"的结构信号，不需要模型自己报数，也不需要序号列存在
（部分投标文件的清单第一列是材料名称、没有序号列，见 score_paddleocr_vl.py 的
同类记录）。

## 为什么是模糊匹配，不是精确匹配（design/26 P1 复核后的修正）

最初版本要求"整个序列能切成 K 等份，且各份逐字节相等"——这是判据**形状**错了，
不是阈值需要调：
- 逐字节相等：每份副本是独立 OCR 出来的，哪怕内容完全一样也会有个别数字/文字
  读法差异，136 行里任何一行任何一格不同就全盘落空。
- 精确整除：各份副本可能各自多读/漏读一两行（宏胜 137、亨通 138 实测），根本
  凑不出"行数相等的 K 份"这个前提。
这两条都是结构前提，不是可以放宽的阈值——加再多样本去"调"，只会更贵地反复
确认同一件事：判据跟输入类型（有噪声的 OCR 文本）不匹配。

改用跟 `block_alignment.py`（几乎同构的问题：找对应的行区块）同一套手法——
`difflib.SequenceMatcher` 序列相似度 + 阈值，而不是布尔相等：**允许错位、
允许长度略有出入**，只要整体内容形态高度相似就判定为同一份的重复副本。

## 两级判据（design/26 P1 二次复核后加）

浦东电缆实测：即便改成模糊匹配，`(name,spec,unit,qty)` 整行内容在两份副本间的
真实相似度只有 0.56（真实边界处，非名义切点误差）——独立 OCR 出来的 `spec`
字段噪声远超预期（同一行"RTTYZ-4x6+E6"在另一份读成"RTTYZ-4x6"，规格文本被
截断），`qty` 单独作为键也只有 0.79（数量是小数，OCR 认错一位小数就整体不等）。
这不是阈值卡太严——降到能让浦东过线的阈值，会让"两段内容完全不同"的正常清单
也大概率过线，等于把误判开了口子。

`block_alignment.py` 早就踩过同一个坑并给出过解法："规格文本不可靠...数量是
双方共享的事实"——但那里比的是数量序列，这里的问题更进一步：**连数量本身
都不可靠**（浦东是电缆米数，小数位多，OCR 误差率比整数数量高）。真正稳的是
`name`（材料大类，如"矿物电缆"/"普通电缆"）这种短、高频、低歧义的类目文字——
浦东实测两份副本的类目序列相似度 0.97，而两个真正不同类目段（如"阀门"×70
vs"管件"×70，一次都不重复）相似度是 0，区分度足够。

所以分两级：先按整行内容（细粒度，区分度最高，干净文档只需要这一级）试一遍；
不过关再退到只按 `name` 值（粗粒度，扛得住 spec/qty 的 OCR 噪声，代价是丢了
同类目内部的区分力）试一遍。宁可先信任细粒度，只在细粒度判不出来时才退化，
不能反过来——细粒度判据本身更不容易谎报"这是副本"。

只看行内容是否重复，不看序号：序号连续性已经由 `draft_integrity.check_sequence_continuity`
独立校验，两者是不同的判据，不要合并成一个函数——序号能连续但内容不重复（正常单份
清单），或者有序号列但重复副本的序号本身也从头再来（这种情况序号本身已经是重复
信号，这里的结构判据依然给得出一致答案，互相印证而不是互相依赖）。

本函数只负责**把行分组**（每行属于第几份），不负责挑哪份入库——挑选早已由
`quote_confirmation_service._dedupe_copies`（design/24 B0）实现：声明总价已知时选
合价之和最接近声明总价的一组，未知时选行数最多的一组。这里分组分不对，B0 那层
再准也没有输入可用；这里分对了，B0 不需要跟着改一个字。
"""
from __future__ import annotations

import difflib

from apps.api.core.domain_config import (
    COPY_MAX_COPIES,
    COPY_MIN_BLOCK_LEN,
    COPY_ROW_SIMILARITY_MIN,
)


def _similarity(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _try_split(keys: list, k: int) -> list[int] | None:
    """按 k 等份的名义切点分段（长度不要求相等，只要求相近），逐段跟第一段比
    相似度。全部达标就返回每行所属第几份，否则返回 None。"""
    n = len(keys)
    nominal = n / k
    if nominal < COPY_MIN_BLOCK_LEN:
        return None
    splits = [round(nominal * i) for i in range(k + 1)]
    splits[-1] = n  # 四舍五入可能让最后一个切点差 1，强制对齐到序列末尾
    segments = [keys[splits[i]:splits[i + 1]] for i in range(k)]
    if any(len(seg) < COPY_MIN_BLOCK_LEN for seg in segments):
        return None
    if all(_similarity(segments[0], seg) >= COPY_ROW_SIMILARITY_MIN for seg in segments[1:]):
        return [i + 1 for i in range(k) for _ in range(len(segments[i]))]
    return None


def detect_copies(row_keys: list[tuple]) -> list[int]:
    """行内容序列 → 每行所属第几份（从 1 开始）。`row_keys[i][0]` 约定为该行的
    类目/名称字段（`name`），供粗粒度判据兜底使用；其余字段不限。

    对 K = COPY_MAX_COPIES..2 依次尝试：先按整行内容（细粒度）分段判定，够不
    上门槛再退到只按 `name` 值（粗粒度）分段判定——理由见模块文档"两级判据"。
    K 从大到小遍历，第一个（不论哪一级）达标的即采信。

    找不到这样的 K 时一律判定单份——**宁可漏判重复，不可把正常清单的巧合重复
    误判成多份**：漏判的后果是 B0 去重没生效、行数偏多，用户在疑点收件箱里
    看得到；误判的后果是正常清单被腰斩，用户看不出少了什么。粗粒度判据额外要求
    类目本身不止一种取值——否则"整份都是同一个类目"这种退化输入会跟自己完全
    重合，不构成"重复"的证据。
    """
    n = len(row_keys)
    if n < COPY_MIN_BLOCK_LEN * 2:
        return [1] * n
    names = [k[0] if k else "" for k in row_keys]
    has_varied_names = len(set(names)) > 1
    for k in range(COPY_MAX_COPIES, 1, -1):
        result = _try_split(row_keys, k)
        if result is not None:
            return result
        if has_varied_names:
            result = _try_split(names, k)
            if result is not None:
                return result
    return [1] * n
