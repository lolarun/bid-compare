"""document_summary.py — design/29 §4：工作台卡片上的一两句话概述。

**不是**让 LLM 重新读原文自由发挥——那样每次都要再花一次识别成本，还有
编造内容的风险（CLAUDE.md："LLM 只能解释确定性结果...不能编造评估事实"）。
输入是已经抽取、已经校验过的结构化字段（project_name/category/row_count
这些），LLM 的活儿窄得很：把这些字段组织成一两句人话，仅此而已。

Prompt 明确约束"只能用给定事实，不能推测、不能加判断"——design/27 红线2
（"系统只陈述事实，从不主动推判断"）同样适用在这里：概述不能出现"质量不错"
"建议优先考虑"这类倾向性表述，只能是"这份文件是什么、有多少条"这类事实
陈述。
"""
from __future__ import annotations

import logging
from typing import Callable, Literal

log = logging.getLogger(__name__)

# 跟 paddle_doc_meta.TextCall 同一个类型/约定——prompt(含事实) -> 模型原始
# 文本响应，可注入、不内嵌网络调用。
SummaryCall = Callable[[str], str]

SummaryKind = Literal["tender", "bid"]

_PROMPT_TEMPLATE = """请把下面这些已经确认过的事实，组织成一两句简短的中文描述，供人快速浏览。

严格规则：
- 只能使用下面给出的事实，不得推测、不得补充没给出的信息。
- 不得包含任何质量评价或倾向性判断（比如"质量不错""建议优先考虑""内容完整"这类话不允许出现）——只陈述事实本身。
- 不确定/未提供的字段直接不提，不要写"未知""待定"这类占位词。
- 直接给结果，不要解释、不要引号包裹、不要开场白。

事实：
{facts}
"""


def _format_facts(kind: SummaryKind, facts: dict) -> str:
    lines: list[str] = []
    if kind == "tender":
        if facts.get("project_name"):
            lines.append(f"项目名称：{facts['project_name']}")
        if facts.get("category"):
            lines.append(f"品类：{facts['category']}")
        if facts.get("row_count") is not None:
            lines.append(f"采购清单行数：{facts['row_count']}")
        if facts.get("deadline"):
            lines.append(f"投标截止时间：{facts['deadline']}")
    else:
        if facts.get("supplier_name"):
            lines.append(f"供应商名称：{facts['supplier_name']}")
        if facts.get("row_count") is not None:
            lines.append(f"报价清单行数：{facts['row_count']}")
        if facts.get("category"):
            lines.append(f"品类：{facts['category']}")
    return "\n".join(lines) if lines else "（无可用事实）"


def compose_summary(kind: SummaryKind, facts: dict, call: SummaryCall | None) -> str:
    """`call=None`（未配置文本客户端）时返回一句模板拼接的兜底描述，不是
    空字符串——design/27 红线1"系统必须陈述已知事实"，没有 LLM 也不能让
    卡片开天窗，退化成纯拼接就是了，不掉信息量太多，只是不够顺口。"""
    facts_text = _format_facts(kind, facts)
    if not facts.get("project_name") and not facts.get("supplier_name"):
        return "（暂无可概述的信息，等待识别完成）"
    if call is None:
        return facts_text.replace("\n", "，")
    try:
        raw = call(_PROMPT_TEMPLATE.format(facts=facts_text))
        summary = (raw or "").strip().strip('"').strip("'")
        return summary or facts_text.replace("\n", "，")
    except Exception as e:                                          # noqa: BLE001
        log.warning("compose_summary 调用失败，退化为拼接：%s", e)
        return facts_text.replace("\n", "，")
