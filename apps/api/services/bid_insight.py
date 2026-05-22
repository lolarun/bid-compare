"""AI-powered bid matrix analysis — generates structured insights via LLM."""

import json
import logging
import time
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

BID_INSIGHT_PROMPT = """你是建筑机电材料采购比价分析专家。请根据以下横向比价矩阵数据，给出专业的分析建议。

## 比价数据

供应商列表：
{suppliers}

材料报价矩阵（单价/偏差%）：
{matrix_text}

汇总：
{totals_text}

## 输出要求

请以 JSON 格式返回分析结果，包含以下字段：
{{
  "overall": "整体评估（1-2 句话，概括本次比价情况）",
  "recommendations": [
    "推荐方案第 1 条（指明主供应商及理由）",
    "推荐方案第 2 条（补充供应商建议）",
    "推荐方案第 3 条（专项采购建议，可选）"
  ],
  "risks": [
    "风险提示第 1 条",
    "风险提示第 2 条"
  ]
}}

注意：
- 推荐理由要具体，引用数据（偏差率、总价、完整度）
- 风险提示要有可操作性（如"建议核实 XX 供应商的 YY 材料报价"）
- 仅返回 JSON，不要解释
"""


def _build_matrix_text(data: dict) -> tuple[str, str, str]:
    """Compress matrix data into readable text tables for the LLM prompt."""
    suppliers = data.get("suppliers", [])
    rows = data.get("rows", [])
    totals = data.get("totals", [])

    # Suppliers
    sup_text = "\n".join(
        f"  {s['letter']}. {s['name']}" for s in suppliers
    )

    # Matrix rows
    header = "材料名称 | 历史均价"
    for s in suppliers:
        header += f" | {s['letter']}(单价/偏差)"
    lines = [header, "-" * len(header)]

    for row in rows[:30]:  # Limit to 30 rows to stay within token budget
        line = f"{row['material_name']}"
        if row.get("spec"):
            line += f" ({row['spec'][:20]})"
        ha = row.get("historical_avg")
        line += f" | ¥{ha['price']:.0f}" if ha else " | —"
        for cell in row.get("suppliers", []):
            if cell["price"] is not None:
                dev = cell.get("deviation_pct")
                dev_str = f"{dev:+.1%}" if dev is not None else "—"
                lowest = " ★" if cell.get("is_lowest") else ""
                line += f" | ¥{cell['price']:.0f} {dev_str}{lowest}"
            else:
                line += " | 未报价"
        lines.append(line)

    matrix_text = "\n".join(lines)
    if len(rows) > 30:
        matrix_text += f"\n...（共 {len(rows)} 条，此处仅展示前 30 条）"

    # Totals
    total_lines = []
    for t in totals:
        sup = next((s for s in suppliers if s["id"] == t["supplier_id"]), None)
        name = sup["name"] if sup else str(t["supplier_id"])
        qc = t.get("quoted_count", "?")
        ac = t.get("anomaly_count", 0)
        total_lines.append(
            f"  {name}: 总价 ¥{t['total']:,.0f}, 平均偏差 {t['avg_deviation']:+.1%}, "
            f"报价 {qc}/{len(rows)} 项, 异常 {ac} 项"
        )
    totals_text = "\n".join(total_lines)

    return sup_text, matrix_text, totals_text


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
        sup_text, matrix_text, totals_text = _build_matrix_text(matrix_data)

        prompt = BID_INSIGHT_PROMPT.format(
            suppliers=sup_text,
            matrix_text=matrix_text,
            totals_text=totals_text,
        )

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
