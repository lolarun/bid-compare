"""preview_ordering.py — design/31 §5：待确认行的影响估算与确认顺序。

回答的问题是"**先去确认哪几行**"，不是"结果是多少"。输入是已经对齐好的
矩阵形状（每个锚点一行，每家供应商一格），输出是一个排好序的待确认队列，
外加一句如实说明估算覆盖了什么、没覆盖什么。

## 为什么这里不能给"名次已定"的结论

本文件的第一版设计想给一条停止规则：「剩余待确认行不足以翻盘，可以不用
再确认了」。**那个断言证明不出来，已经撤回**（design/31 §5.1）：一行没读
出来的价格可以是任何数，拿别家对同一锚点的报价推出来的区间是"同行离散度
的估算"，不是对真值的界。把估算说成确定性，正是 CLAUDE.md 禁止的
"靠下游猜测抬高质量分层"。

所以这里给的一切都带着 `estimated` / `unbounded` 的分类往下走，调用方
必须把这个分类跟数字一起显示，不能只显示数字。

## 纯函数，不碰数据库

不 import 任何 model、不接 Session。输入是普通 dataclass，便于直接对
"排序对不对、无法估算的行有没有被藏起来"写断言——这两件事正是这个模块
唯一的职责。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 影响估算的分类。两者的区别必须一路带到界面上：
#  estimated —— 有 ≥2 家同行报了同一锚点，可以给一个"合理波动区间"的估算；
#  unbounded —— 同行不足以形成区间，连估算都给不了。**不得退化成 0**：
#               没人读得出的行恰恰最可能是贵的那一行。
ImpactKind = Literal["estimated", "unbounded"]


@dataclass(frozen=True)
class PreviewCell:
    """一个锚点上某一家供应商的格子。

    两个字段回答**两个不同的问题**，初版把它们挤进了一个：

    - `unit_price` —— 这一格有没有可用于估算的单价。None = 没有。
    - `confirmable` —— 这一格是不是**人可以去确认**的待办。

    初版只有 `unit_price`，用 `is None` 同时表示"没有价"和"要人确认"。
    实测代价：同一份数据里 quoted 169 / missing 50 / aggregated 36 /
    pending 9，队列列出 95 条，其中只有 9 条是人能动的。`missing` 是
    "供应商没报这一行"——它确实没有价，但用户对它无能为力，列进待办等于
    让人去确认一件不存在的事。
    """
    supplier_key: str
    unit_price: float | None
    #: 默认 False：新增字段时忘了传，结果是"这一格不进待办队列"——漏报一条
    #: 待办，比凭空多报一屏待办容易发现，也容易补。
    confirmable: bool = False


@dataclass(frozen=True)
class PreviewRow:
    anchor_key: str
    qty: float | None
    cells: tuple[PreviewCell, ...]


@dataclass(frozen=True)
class PendingImpact:
    """一条待确认格子的影响估算。"""
    anchor_key: str
    supplier_key: str
    kind: ImpactKind
    #: kind="estimated" 时是估算的波动幅度（qty × 同行价差）；
    #: kind="unbounded" 时恒为 None——不允许用 0 冒充"影响很小"。
    swing: float | None
    #: 该行的体量（qty × 同行均价）。只有一家同行报价时波动无从估算，但体量
    #: 仍然知道，用来在 unbounded 组内排序。都不知道时为 None。
    magnitude: float | None
    peer_count: int


@dataclass
class PreviewOrdering:
    """排好序的确认队列 + 如实的覆盖说明。"""
    queue: list[PendingImpact] = field(default_factory=list)
    #: 能给出估算的那部分，波动幅度合计。
    estimated_total_swing: float = 0.0
    #: 连估算都给不了的格子数。>0 时调用方**不得**声称名次不会变。
    unbounded_count: int = 0

    @property
    def pending_count(self) -> int:
        return len(self.queue)


def _peer_prices(row: PreviewRow, supplier_key: str) -> list[float]:
    return [c.unit_price for c in row.cells
            if c.supplier_key != supplier_key and c.unit_price is not None]


def _impact(row: PreviewRow, cell: PreviewCell) -> PendingImpact:
    peers = _peer_prices(row, cell.supplier_key)
    qty = row.qty
    if qty is None or not peers:
        # 数量缺失或没有任何同行价 → 波动和体量都无从谈起。
        return PendingImpact(row.anchor_key, cell.supplier_key,
                             "unbounded", None, None, len(peers))
    magnitude = qty * (sum(peers) / len(peers))
    if len(peers) < 2:
        # 只有一家同行 = 只有一个点，构不成区间。体量知道，波动不知道——
        # 不能把"区间宽度 0"当成"影响为 0"，那是把无知说成确定。
        return PendingImpact(row.anchor_key, cell.supplier_key,
                             "unbounded", None, magnitude, len(peers))
    return PendingImpact(row.anchor_key, cell.supplier_key, "estimated",
                         qty * (max(peers) - min(peers)), magnitude, len(peers))


def build_ordering(rows: list[PreviewRow]) -> PreviewOrdering:
    """矩阵行 → 待确认队列（design/31 §5.3）。

    排序：`unbounded` 一律排在前面（读不出来的行最值得先看，且系统对它最
    没把握），组内按体量降序、体量未知的再排在最前；其余按估算波动降序。
    """
    impacts = [
        _impact(row, cell)
        for row in rows
        for cell in row.cells
        if cell.confirmable
    ]

    def sort_key(i: PendingImpact):
        if i.kind == "unbounded":
            # 体量未知 (-1) 排在体量已知之前；组内体量大的在前。
            return (0, 0 if i.magnitude is None else 1, -(i.magnitude or 0.0))
        return (1, 1, -(i.swing or 0.0))

    impacts.sort(key=sort_key)
    return PreviewOrdering(
        queue=impacts,
        estimated_total_swing=round(
            sum(i.swing or 0.0 for i in impacts if i.kind == "estimated"), 2),
        unbounded_count=sum(1 for i in impacts if i.kind == "unbounded"),
    )


def describe(ordering: PreviewOrdering, leader_gap: float | None) -> str:
    """给界面用的一句话。措辞受 §5.1 约束：估算就说估算，无法估算就说
    无法估算，**任何情况下都不说"名次已经确定"**。

    `leader_gap` = 当前第 1 名与第 2 名的评标总价差；不足两家可比时传 None。
    """
    if not ordering.queue:
        return "没有待确认项。"
    parts = [f"剩余 {ordering.pending_count} 项待确认"]
    if ordering.estimated_total_swing:
        parts.append(f"按同行报价区间估算最多影响 ¥{ordering.estimated_total_swing:,.2f}")
    if ordering.unbounded_count:
        parts.append(f"另有 {ordering.unbounded_count} 项无法估算")
    line = "；".join(parts) + "。"
    if leader_gap is None:
        return line
    line = f"当前第 1 名领先第 2 名 ¥{leader_gap:,.2f}。" + line
    if ordering.unbounded_count:
        # 有无法估算的行时，对"会不会翻盘"保持沉默是唯一诚实的做法。
        return line + "存在无法估算的项，因此不对名次是否会变化下结论。"
    if ordering.estimated_total_swing < leader_gap:
        return line + "按估算，剩余待确认项不足以改变名次（估算值，非保证）。"
    return line + "按估算，剩余待确认项足以改变名次，建议按上方顺序逐项确认。"
