"""copy_detect.py — 结构化副本检测：同一份清单在文档内重复出现（正本/副本、
汇总/明细）时，按行内容识别第几份，产出 `copy_no`。

design/26 §5：这是 Paddle 与 qwen 两条识别路径共用的基础设施，不是 Paddle 专属。
qwen 路径今天完全依赖 `PROMPT_QUOTE_CSV` 第 3 条提示词——模型要自己数"这是第几份"，
不总照做（提示词是请求，不是保证）。这里给一个不依赖模型自觉性的结构判据：
**整份行序列里，是否存在若干个等长的连续区块，内容彼此相同**——那正是"同一张表
被原样重复打印了 K 次"的结构信号，不需要模型自己报数，也不需要序号列存在
（部分投标文件的清单第一列是材料名称、没有序号列，见 score_paddleocr_vl.py 的
同类记录）。

只看行内容是否重复，不看序号：序号连续性已经由 `draft_integrity.check_sequence_continuity`
独立校验，两者是不同的判据，不要合并成一个函数——序号能连续但内容不重复（正常单份
清单），或者有序号列但重复副本的序号本身也从头再来（这种情况序号本身已经是重复
信号，这里的结构判据依然给得出一致答案，互相印证而不是互相依赖）。
"""
from __future__ import annotations

# 至少要这么长的连续区块相等，才采信"这是重复副本"——太短的窗口（比如 1-2 行）
# 在正常清单里也会偶然重复（同规格不同批次），不能算副本边界。
_MIN_BLOCK_LEN = 4

# 允许的最大副本数。清单当前实测最多两三份（正本+副本×1-2），设更高的上限只是防呆，
# 不是产品约束。
_MAX_COPIES = 6


def detect_copies(row_keys: list[tuple]) -> list[int]:
    """行内容序列 → 每行所属第几份（从 1 开始）。

    算法：尝试把整个序列切成 K 等份（K = 2, 3, ...），检查各份是否与第一份
    逐行相同；K 最大且能整除、且各份相等的，就是真实副本数。找不到这样的 K
    （包括长度不能被任何候选 K 整除、或没有任何一份完全匹配）时，一律判定
    单份——**宁可漏判重复，不可把正常清单的巧合重复误判成多份**：漏判的后果
    是 B0 去重没生效、行数偏多，用户在疑点收件箱里看得到；误判的后果是
    正常清单被腰斩，用户看不出少了什么。
    """
    n = len(row_keys)
    if n < _MIN_BLOCK_LEN * 2:
        return [1] * n
    for k in range(_MAX_COPIES, 1, -1):
        if n % k != 0:
            continue
        block = n // k
        if block < _MIN_BLOCK_LEN:
            continue
        blocks = [row_keys[i * block:(i + 1) * block] for i in range(k)]
        if all(b == blocks[0] for b in blocks[1:]):
            return [i + 1 for i in range(k) for _ in range(block)]
    return [1] * n
