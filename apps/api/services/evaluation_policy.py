"""EvaluationPolicy — 招标文件驱动的评标规则（不是全系统固定最低价规则）。

来源：每个项目的评标方法以**招标文件条款**为准，不得由系统硬编码"最低价中标"或
自造权重定标。当招标文件未给出评分公式/权重时，系统只能输出**确定性的价格事实**
（评标总价排名、价格优选候选人）和**非价格因素证据/缺失项**，并明确最终结论
"需招标领导小组确认"——绝不冒充已完成官方综合评分。

当前默认策略对应 Project 66 招标文件第十/十一条：
  - 评标方法 = 合理低价评标价法（最低报价不保证中标）；
  - 授标方式 = 单一中标人（非拆单组合）；
  - 综合评价八项（企业规模/供货渠道/产品质量/价格/售后/工期/垫资能力/承诺），未给权重；
  - 最终由招标领导小组确定。

设计为纯数据 dataclass，无 I/O。未来可由招标文件解析填充；现阶段用保守默认值，
这些默认值恰好满足"不冒充定标、不拆单、不造权重"的安全要求，对任何未知招标文件都安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 招标文件第十条列明的八项综合评价因素（顺序与文件一致）。
DEFAULT_NON_PRICE_FACTORS: list[str] = [
    "企业规模",
    "供货渠道",
    "产品质量",
    "价格",          # 价格本身也是综合评价的一维，但有确定性数据
    "售后服务",
    "工期",
    "垫资能力",
    "承诺",
]


@dataclass(frozen=True)
class EvaluationPolicy:
    """一份招标文件的评标政策。"""

    method: str = "reasonable_low_price"          # 合理低价评标价法（非 lowest_price）
    award_mode: str = "single_supplier"           # single_supplier | split_award
    lowest_price_wins: bool = False               # 最低价是否直接中标 → 本项目 False
    factors: tuple[str, ...] = field(default_factory=lambda: tuple(DEFAULT_NON_PRICE_FACTORS))
    weights: dict[str, float] | None = None       # 招标文件未给权重 → None（禁止系统自造）
    final_decision_requires_committee: bool = True  # 最终由招标领导小组确定

    @property
    def allows_split_award(self) -> bool:
        """是否允许拆单/分项授标（决定前端是否展示最优组合总价）。"""
        return self.award_mode == "split_award"

    @property
    def can_auto_declare_winner(self) -> bool:
        """系统是否可自动判定中标人。

        仅当招标文件明确最低价中标且无需委员会时为真——本项目恒 False。
        无权重 / 需委员会 / 非最低价法 任一成立都不可自动定标。
        """
        return (
            self.lowest_price_wins
            and self.weights is not None
            and not self.final_decision_requires_committee
        )

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "award_mode": self.award_mode,
            "lowest_price_wins": self.lowest_price_wins,
            "factors": list(self.factors),
            "weights": self.weights,
            "final_decision_requires_committee": self.final_decision_requires_committee,
            "allows_split_award": self.allows_split_award,
            "can_auto_declare_winner": self.can_auto_declare_winner,
        }


# 默认（保守）策略：对任何未知招标文件安全——不自动定标、不拆单、不造权重。
DEFAULT_EVALUATION_POLICY = EvaluationPolicy()


def get_evaluation_policy(project_id: int | None = None) -> EvaluationPolicy:
    """返回项目的评标政策。

    现阶段所有项目用保守默认策略（合理低价 / 单一授标 / 无权重 / 需委员会）。
    未来可按 project_id 从招标文件解析结果加载；在那之前绝不返回会自动定标的策略。
    """
    return DEFAULT_EVALUATION_POLICY
