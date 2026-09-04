"""AI-powered bid matrix analysis — generates structured insights via LLM."""

import json
import logging
import time
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

BID_INSIGHT_PROMPT = """你是建筑机电材料招投标评标解读助手。你的职责是**解释系统已计算的确定性结果**，
绝不自行定标、不改选供应商、不把评标方法改成"最低价中标"。

## 本项目评标规则（来自招标文件，最高优先级，不得违反）
{policy_text}

## 系统已计算的确定性结果（你只能据此解释，不得改动）
评标总价排名（招标数量 × 供应商有效含税单价；最低报价**不保证**中标）：
{ranking_text}

价格优选候选人（仅价格维度，**非中标结论**）：{price_preferred}

各投标人评标情况：
{eval_text}

共同可比金额（各入排名投标人均可评标的行）：{common_text}

非价格因素（招标文件八项，系统暂无结构化证据）：
{factors_text}

系统识别的风险：
{risks_text}

## 输出要求（严格 JSON）
{{
  "overall": "1-2 句：说明评标方法为合理低价评标价法、最低价不保证中标、最终由招标领导小组确定",
  "recommendations": [
    "解释『价格优选候选人』的依据（引用评标总价、完整度），并明确这只是价格维度、需综合评审",
    "明确综合评审（企业规模/供货渠道/质量/售后/工期/垫资/承诺）证据不足，待招标领导小组确认",
    "（如有未决：说明未决行与金额影响，建议补充材料）"
  ],
  "risks": ["逐条复述/细化系统风险，给出可操作核实建议"]
}}

## 红线（违反即错误）
- 不得输出"中标""定标""确定中标人"等表述；最终结论一律"需招标领导小组确认"。
- 不得自行给出综合评分或权重；招标文件未提供评分公式。
- 不得改选供应商、不得宣称最低价即中标、不得生成拆单/最优组合方案。
- 仅返回 JSON，不要额外解释。
"""


def _build_policy_text(data: dict) -> str:
    p = data.get("evaluation_policy") or {}
    method = p.get("method", "unknown")
    award_mode = p.get("award_mode", "unknown")
    factors = "、".join(p.get("factors") or [])
    if method == "unknown":
        method_desc = "未确认（招标文件评标法尚未解析或人工确认）"
    else:
        method_desc = f"{method}（合理低价评标价法，最低报价不保证中标）"
    if award_mode == "unknown":
        award_desc = "未确认（授标方式尚未确认）"
    elif award_mode == "split_award":
        award_desc = f"{award_mode}（允许拆单分项授标）"
    else:
        award_desc = f"{award_mode}（单一中标人，不允许拆单分项授标）"
    factors_desc = factors if factors else "未确认"
    return (
        f"- 评标方法：{method_desc}\n"
        f"- 授标方式：{award_desc}\n"
        f"- 综合评价因素（未给权重）：{factors_desc}\n"
        f"- 最终由招标领导小组确定：{'是' if p.get('final_decision_requires_committee', True) else '否'}"
    )


def _build_matrix_text(data: dict) -> dict:
    """Compress evaluation context into readable text blocks for the LLM prompt."""
    suppliers = data.get("suppliers", [])
    name_by = {s["id"]: s["name"] for s in suppliers}

    ranking = data.get("price_ranking") or []
    if ranking:
        ranking_text = "\n".join(
            f"  {i}. {r.get('name')}：评标总价 ¥{r.get('evaluated_total', 0):,.0f}"
            f"（确认 {r.get('confirmed_lines')}/{r.get('total_anchors')} 行）"
            for i, r in enumerate(ranking, 1)
        )
    else:
        ranking_text = "  （无投标人可形成完整含税评标总价）"

    pc = data.get("price_preferred_candidate")
    price_preferred = (
        f"{pc.get('name')}（评标总价 ¥{pc.get('evaluated_total', 0):,.0f}）" if pc else "无（条件不足）"
    )

    eval_lines = []
    for s in data.get("supplier_evaluation", []):
        eval_lines.append(
            f"  {s.get('name')}：评标总价 ¥{s.get('evaluated_total', 0):,.0f}，"
            f"确认 {s.get('confirmed_lines')}/{s.get('total_anchors')} 行，"
            f"数量冲突 {s.get('qty_conflict_lines')} 行，"
            f"未决 {s.get('undecided_lines')} 行(≈¥{s.get('undecided_amount', 0):,.0f})，"
            f"含税口径确认={s.get('basis_confirmed')}，核验={s.get('checksum_status')}"
        )
    eval_text = "\n".join(eval_lines) or "  （无）"

    cc = data.get("common_comparable") or {}
    common_text = (
        f"{cc.get('line_count', 0)} 行可共同比价；小计："
        + "，".join(f"{name_by.get(int(k), k)} ¥{v:,.0f}" for k, v in (cc.get("subtotals") or {}).items())
        if cc.get("subtotals") else f"{cc.get('line_count', 0)} 行"
    )

    factors = data.get("non_price_factors") or []
    factors_text = "\n".join(
        f"  - {f.get('factor')}：{f.get('evidence_status', 'missing')}" for f in factors
    ) or "  （招标文件八项，证据待补）"

    risks = data.get("risks") or []
    risks_text = "\n".join(f"  - {r}" for r in risks) or "  （无）"

    return {
        "policy_text": _build_policy_text(data),
        "ranking_text": ranking_text,
        "price_preferred": price_preferred,
        "eval_text": eval_text,
        "common_text": common_text,
        "factors_text": factors_text,
        "risks_text": risks_text,
    }


def generate_bid_insight(
    matrix_data: dict,
    client: OpenAI,
    model: str = "qwen-plus",
    timeout: int = 30,
) -> dict[str, Any]:
    """Call Qwen text model to analyze the bid matrix and return structured insights.

    Returns:
        {"overall": str, "recommendations": list[str], "risks": list[str],
         "tokens_used": int, "duration_ms": int}
        On failure returns {"overall": "", "recommendations": [], "risks": [],
                           "error": str}
    """
    try:
        blocks = _build_matrix_text(matrix_data)
        prompt = BID_INSIGHT_PROMPT.format(**blocks)

        t0 = time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        raw = resp.choices[0].message.content or "{}"
        duration_ms = int((time.time() - t0) * 1000)

        # Parse JSON (tolerant of markdown fences)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            text = text[i : j + 1]
        data = json.loads(text)

        tokens = 0
        if resp.usage:
            tokens = getattr(resp.usage, "total_tokens", 0)

        return {
            "overall": data.get("overall", ""),
            "recommendations": data.get("recommendations", []),
            "risks": data.get("risks", []),
            "tokens_used": tokens,
            "duration_ms": duration_ms,
        }

    except Exception as e:
        log.warning("bid_insight LLM call failed: %s", e)
        return {
            "overall": "",
            "recommendations": [],
            "risks": [],
            "error": str(e),
        }
