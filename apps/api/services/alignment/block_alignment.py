"""block_alignment.py — 报价清单 → 招标清单的两级对齐：先块级，再块内按行序。

## 为什么需要块级这一层

报价清单的**物理顺序不等于招标清单顺序**。实测某份投标文件把普通电缆印在 PDF 第 2-7 页、
矿物电缆印在第 8-10 页，而采购清单的序号是矿物 1-44、普通 45-136。直接按文档行序对齐，
严格位置命中 **0%**、保序命中 67%；先把块对上再在块内对齐，保序命中升到 **99%**。

## 为什么块级键用数量序列，不用金额、不用规格文本

- **金额不可用**：招标采购清单只有序号/名称/规格/单位/数量，**没有价格**——价格是每家
  自己报的。拿金额对块等于拿答案对答案，在生产里根本取不到这个输入。
- **规格文本不可靠**：实测同一批行里 `RTXMY` 被读成 `RTXMV`、`4*300` 被拆成 `4*30+0`。
  文本适合做校验，不适合做主键。
- **数量是双方共享的事实**：招标方给数量，各家报同一套数量。实测四份文档的块级数量序列
  相似度 0.93~1.00。

但数量序列**不是普适解**：数量大量重复时（阀门清单里大量「数量=1」）序列几乎没有区分度。
所以确定性判定不了的块，交给 LLM 做**块级对应**这一个决定——那是几个决定，不是 136 行的
重排，且结果可验证（行数、数量序列都能核）、可审计、可回退。

## LLM 的边界

**只判块与块的对应关系**，不重排行、不改值、不补数。行级的事全部由确定性的保序对齐完成，
冲突行单独 pending（CLAUDE.md §4：LLM 只解释确定性结果，不产生评审事实）。
"""
from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

from apps.api.core.domain_config import (
    BLOCK_ASSIGN_AMBIGUITY_MARGIN,
    BLOCK_QTY_SIMILARITY_MIN,
    BLOCK_ROW_CONFLICT_MAX_RATE,
    SEQ_QTY_TOLERANCE,
)

log = logging.getLogger(__name__)

DETERMINISTIC = "qty_sequence"
LLM_RESOLVED = "llm_block_match"
ORDER_FALLBACK = "order_fallback"


# ─── 行与块 ──────────────────────────────────────────────────────────────────

@dataclass
class Row:
    """对齐只需要这些字段。价格**不参与对齐**，只在冲突判定时作为佐证。"""
    doc_index: int                  # 全局文档行序（页码×页内行序推出，自动标识）
    category: str = ""              # 类目/名称列——块的划分依据
    spec: str = ""
    unit: str = ""
    qty: float | None = None
    payload: dict = field(default_factory=dict)   # 原始行，原样带走，不修改


@dataclass
class Block:
    key: str                        # 类目取值
    rows: list[Row]

    @property
    def qtys(self) -> list:
        return [r.qty for r in self.rows]

    def summary(self, sample: int = 3) -> dict:
        """给 LLM 看的块摘要——只给结构信息，不给价格。"""
        return {"category": self.key, "row_count": len(self.rows),
                "qty_head": self.qtys[:sample], "qty_tail": self.qtys[-sample:],
                "spec_head": [r.spec for r in self.rows[:sample]]}


def split_blocks(rows: Sequence[Row]) -> list[Block]:
    """按类目取值的**变化**切块，保持文档行序。

    注意是「变化」而不是「分组」：同一类目在文档里出现两段时应当是两个块，
    合并它们会抹掉顺序信息，而顺序正是块内对齐的依据。
    """
    ordered = sorted(rows, key=lambda r: r.doc_index)
    out: list[Block] = []
    for r in ordered:
        k = (r.category or "").strip()
        if not out or out[-1].key != k:
            out.append(Block(key=k, rows=[]))
        out[-1].rows.append(r)
    return out


def drop_section_headers(rows: Sequence[Row]) -> tuple[list[Row], list[Row]]:
    """分出「看起来是段落标题、被误判成明细」的行：无数量、无规格。

    实测某份文档出现两行只有类目名（如「电缆头」）、数量与价格全空的行，
    它们让行数从 136 变成 138。**返回而不是丢弃**——调用方要能看到被分走了什么。
    """
    body, headers = [], []
    for r in rows:
        if r.qty is None and not (r.spec or "").strip():
            headers.append(r)
        else:
            body.append(r)
    return body, headers


# ─── 块级对应 ────────────────────────────────────────────────────────────────

def qty_similarity(a: Sequence, b: Sequence) -> float:
    """两段数量序列的相似度。用序列而非集合：顺序本身是信息。"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, list(a), list(b), autojunk=False).ratio()


@dataclass
class BlockAssignment:
    anchor_key: str
    anchor_rows: list[Row]
    quote_rows: list[Row] = field(default_factory=list)
    score: float = 0.0
    method: str = ORDER_FALLBACK
    note: str = ""

    def to_dict(self) -> dict:
        return {"anchor_block": self.anchor_key, "anchor_rows": len(self.anchor_rows),
                "quote_rows": len(self.quote_rows), "score": round(self.score, 3),
                "method": self.method, "note": self.note}


# LLM 解析器契约：给双方块摘要，返回 {报价块下标 -> 锚点块下标}。
# 判断不了就返回空 dict —— **不允许猜**，猜错在下游是不可见的。
BlockResolver = Callable[[list[dict], list[dict]], dict[int, int]]


def assign_blocks(quote_blocks: list[Block], anchor_blocks: list[Block],
                  *, resolver: BlockResolver | None = None) -> list[BlockAssignment]:
    """把报价块指派给锚点块。**行数守恒**：每一行都必须出现在结果里。

    两级：
      1. 数量序列相似度 —— 枚举报价侧的**连续块段**（一个锚点章节可能被拆成几个小块，
         实测某份把「预分支电缆头」单独切了 5 段），取相似度最高且不歧义的一段；
      2. 剩下判不了的，交 resolver（LLM）做块级对应；resolver 缺席或拒答时按文档顺序回退，
         并标注 method=order_fallback —— 不确定性必须留在数据里，不能消失。
    """
    n = len(quote_blocks)
    segs: dict[tuple[int, int], list[Row]] = {}
    for i in range(n):
        acc: list[Row] = []
        for j in range(i, n):
            acc = acc + quote_blocks[j].rows
            segs[(i, j)] = acc

    used: set[int] = set()
    out: list[BlockAssignment] = []
    for ab in anchor_blocks:
        cands = sorted(
            ((qty_similarity([r.qty for r in rows], ab.qtys), rng)
             for rng, rows in segs.items()
             if not any(k in used for k in range(rng[0], rng[1] + 1))),
            key=lambda x: -x[0])
        a = BlockAssignment(anchor_key=ab.key, anchor_rows=ab.rows)
        if cands:
            best_score, best_rng = cands[0]
            second = cands[1][0] if len(cands) > 1 else 0.0
            confident = (best_score >= BLOCK_QTY_SIMILARITY_MIN
                         and best_score - second >= BLOCK_ASSIGN_AMBIGUITY_MARGIN)
            if confident:
                used.update(range(best_rng[0], best_rng[1] + 1))
                a.quote_rows, a.score, a.method = segs[best_rng], best_score, DETERMINISTIC
            else:
                a.note = (f"数量序列不足以判定（最优 {best_score:.3f}，次优 {second:.3f}）"
                          if cands else "无可用候选")
        out.append(a)

    undecided = [k for k, a in enumerate(out) if not a.quote_rows]
    leftover = [k for k in range(n) if k not in used]
    if undecided and leftover and resolver is not None:
        mapping = _resolve_with_llm(resolver, quote_blocks, leftover, out, undecided)
        for qi, ai in mapping.items():
            if qi in leftover and ai in undecided and not out[ai].quote_rows:
                out[ai].quote_rows = quote_blocks[qi].rows
                out[ai].method, out[ai].score = LLM_RESOLVED, 0.0
                out[ai].note = "块级对应由 LLM 判定；行级仍按文档行序对齐"
                used.add(qi)
        leftover = [k for k in leftover if k not in used]
        undecided = [k for k, a in enumerate(out) if not a.quote_rows]

    # 兜底：剩余块按文档顺序补给仍然空着的锚点块。一行都不能丢。
    for idx, ai in enumerate(undecided):
        take = leftover if idx == len(undecided) - 1 else leftover[:1]
        out[ai].quote_rows = [r for k in take for r in quote_blocks[k].rows]
        out[ai].method = ORDER_FALLBACK
        out[ai].note = (out[ai].note + "；" if out[ai].note else "") + "按文档顺序回退，未经确认"
        leftover = [k for k in leftover if k not in take]
    if leftover and out:
        out[-1].quote_rows += [r for k in leftover for r in quote_blocks[k].rows]
        out[-1].note += "；另有未指派块并入本块尾部"
    return out


def _resolve_with_llm(resolver: BlockResolver, quote_blocks: list[Block],
                      leftover: list[int], assignments: list[BlockAssignment],
                      undecided: list[int]) -> dict[int, int]:
    q = [dict(quote_blocks[k].summary(), index=k) for k in leftover]
    a = [{"index": i, "category": assignments[i].anchor_key,
          "row_count": len(assignments[i].anchor_rows),
          "qty_head": [r.qty for r in assignments[i].anchor_rows[:3]],
          "qty_tail": [r.qty for r in assignments[i].anchor_rows[-3:]]}
         for i in undecided]
    try:
        return {int(k): int(v) for k, v in (resolver(q, a) or {}).items()}
    except Exception:                                   # noqa: BLE001
        log.exception("block resolver failed; falling back to document order")
        return {}


# ─── 块内按行序对齐 ──────────────────────────────────────────────────────────

@dataclass
class RowPair:
    quote: Row | None
    anchor: Row | None
    conflicts: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.quote is None or self.anchor is None:
            return "unmatched"
        return "conflict" if self.conflicts else "aligned"


def _row_conflicts(q: Row, a: Row) -> list[str]:
    """逐行冲突判定。只用双方都该有的事实：数量与单位。价格不参与——
    价格是各家自己的，不同不代表对错。"""
    bad = []
    # 与 anchor_match.py/bid_evaluation.py 共用同一个数量比较容差（评审 D4：
    # 此前这里硬编码 1e-6，比另两处的 0.001 严 1000 倍，会把另两处判齐的行
    # 判成冲突）。
    if q.qty is not None and a.qty is not None and abs(q.qty - a.qty) > SEQ_QTY_TOLERANCE:
        bad.append("qty")
    qu, au = (q.unit or "").strip(), (a.unit or "").strip()
    if qu and au and qu != au:
        bad.append("unit")
    return bad


def align_in_order(quote_rows: Sequence[Row], anchor_rows: Sequence[Row]) -> list[RowPair]:
    """块内保序对齐：允许插入/删除，**不允许乱序**。

    为什么不用等位对齐：多一行少一行会让后面全部错位，实测某份多 2 行就把命中率从
    99% 打到 34%。保序对齐把多/少的行局部化成 unmatched，不牵连其余。
    """
    qk = [(r.qty, (r.unit or "").strip()) for r in quote_rows]
    ak = [(r.qty, (r.unit or "").strip()) for r in anchor_rows]
    pairs: list[RowPair] = []
    sm = difflib.SequenceMatcher(None, qk, ak, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                q, a = quote_rows[i1 + k], anchor_rows[j1 + k]
                pairs.append(RowPair(q, a, _row_conflicts(q, a)))
        else:
            # 替换段按位置就近配对，多出来的两侧各自 unmatched —— 不猜、不丢
            span = min(i2 - i1, j2 - j1)
            for k in range(span):
                q, a = quote_rows[i1 + k], anchor_rows[j1 + k]
                pairs.append(RowPair(q, a, _row_conflicts(q, a) or ["out_of_sequence"]))
            for k in range(span, i2 - i1):
                pairs.append(RowPair(quote_rows[i1 + k], None, ["quote_only"]))
            for k in range(span, j2 - j1):
                pairs.append(RowPair(None, anchor_rows[j1 + k], ["anchor_only"]))
    return pairs


# ─── 全流程 ──────────────────────────────────────────────────────────────────

@dataclass
class AlignmentResult:
    pairs: list[RowPair] = field(default_factory=list)
    blocks: list[BlockAssignment] = field(default_factory=list)
    section_headers: list[Row] = field(default_factory=list)

    @property
    def aligned(self) -> list[RowPair]:
        return [p for p in self.pairs if p.status == "aligned"]

    @property
    def pending(self) -> list[RowPair]:
        """冲突行与单边行都进 pending：可展示、可人工裁决，**不进正式比价**。"""
        return [p for p in self.pairs if p.status != "aligned"]

    def to_dict(self) -> dict:
        anchors = sum(len(b.anchor_rows) for b in self.blocks)
        return {
            "anchor_rows": anchors,
            "aligned": len(self.aligned),
            "aligned_rate": round(len(self.aligned) / anchors, 4) if anchors else 0.0,
            "pending": len(self.pending),
            "section_headers_excluded": len(self.section_headers),
            "blocks": [b.to_dict() for b in self.blocks],
            "needs_review": [b.to_dict() for b in self.blocks
                             if b.method != DETERMINISTIC],
        }


def align_quote_to_anchors(quote_rows: Sequence[Row], anchor_rows: Sequence[Row],
                           *, resolver: BlockResolver | None = None) -> AlignmentResult:
    """完整流程：剔段落标题 → 切块 → 块级对应 → 块内保序对齐 → 冲突行 pending。"""
    body, headers = drop_section_headers(quote_rows)
    assignments = assign_blocks(split_blocks(body), split_blocks(anchor_rows),
                                resolver=resolver)
    pairs: list[RowPair] = []
    for a in assignments:
        rows = align_in_order(a.quote_rows, a.anchor_rows)
        conflicts = sum(1 for p in rows if p.status != "aligned")
        if a.anchor_rows and conflicts / len(a.anchor_rows) > BLOCK_ROW_CONFLICT_MAX_RATE:
            a.note = ((a.note + "；") if a.note else "") + \
                f"块内冲突率 {conflicts / len(a.anchor_rows):.0%}，块级对应本身存疑"
        pairs.extend(rows)
    return AlignmentResult(pairs=pairs, blocks=assignments, section_headers=headers)


# ─── LLM 块级解析器（生产实现）──────────────────────────────────────────────

_BLOCK_PROMPT = """你在做投标报价清单与招标采购清单的**分段对应**。

下面是两侧的分段摘要。请判断报价侧的每一段对应招标侧的哪一段。
判断依据：类目名称的业务含义、行数、数量序列的形态。

只返回 JSON，形如 {"报价段index": 招标段index}。
无法确定的段**不要出现在结果里**——宁可留空也不要猜。不要输出任何其他内容。

报价侧分段：
%s

招标侧分段：
%s"""


def dashscope_block_resolver(model: str | None = None) -> BlockResolver:
    """用集中式 LLM 客户端做块级对应。只判分段对应，不碰行。

    `model=None` 时由 `get_text_client()` 决定（design/41 统一入口）；显式传
    模型名仍然有效，留给测试和一次性排查用。函数名保留 `dashscope_` 前缀是
    因为它已经是多处调用方的公开名字，改名是另一件事——**名字里的供应商不再
    代表实际供应商**，这一点写在这里免得误导。
    """
    def _resolve(quote_summaries: list[dict], anchor_summaries: list[dict]) -> dict[int, int]:
        from apps.api.services.llm_provider import get_text_client
        got = get_text_client()
        if got is None:
            return {}
        client, _default_model = got
        _model = model or _default_model
        prompt = _BLOCK_PROMPT % (
            json.dumps(quote_summaries, ensure_ascii=False, indent=1),
            json.dumps(anchor_summaries, ensure_ascii=False, indent=1))
        r = client.chat.completions.create(
            model=_model, temperature=0,
            messages=[{"role": "user", "content": prompt}])
        text = (r.choices[0].message.content or "").strip()
        text = text[text.find("{"):text.rfind("}") + 1] if "{" in text else "{}"
        return {int(k): int(v) for k, v in json.loads(text).items()}
    return _resolve
