"""AI-powered bid alignment — detects misaligned quote rows across suppliers.

When multiple suppliers quote on the same tender, their PDFs use different names
for the same item. This service asks an LLM to group quotes that refer to the
same tender item and detect field errors (e.g. total in unit_price field).

Flow: compare page → POST /api/analysis/bid-alignment/suggest → LLM → groups + fixes
      → user confirms → POST /api/analysis/bid-alignment/apply → persisted mapping
"""

import json
import logging
import time
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

BID_ALIGNMENT_PROMPT = """你是建筑机电材料采购比价对齐分析专家。

## 任务

以下是同一个项目、同一品类下多家供应商的报价识别数据。请分析：

1. **对齐分组**：哪些行实际是同一招标清单项？给每组建议一个标准名称、标准规格、单位、数量。
2. **字段纠错**：哪些行的 unit_price 疑似为合价（即 unit_price ≈ total_price 且 qty > 1）？

## 报价数据

品类：{category}
供应商：{supplier_names}

{rows_text}

## 输出要求

请以 JSON 格式返回：
{{
  "groups": [
    {{
      "suggested_name": "标准材料名称",
      "suggested_spec": "标准规格",
      "confidence": 0.92,
      "reason": "分组原因（引用名称、规格、数量等可核对字段）",
      "items": [
        {{"quote_id": 123, "supplier_id": 7, "action": "align"}},
        {{"quote_id": 456, "supplier_id": 8, "action": "align", "spec_note": "型号差异保留"}}
      ]
    }}
  ],
  "field_fixes": [
    {{
      "quote_id": 789,
      "field": "unit_price",
      "current": 1802,
      "suggested": 106,
      "confidence": 0.86,
      "reason": "数量为17，total_price 为1802，当前 unit_price 等于合价，疑似列错位。"
    }}
  ]
}}

注意：
- 仅当名称/规格明显指向同一清单项时才合并，不确定的不要合并
- 数量/单位不一致时降低置信度并说明
- 单价=合价判断：当 unit_price ≈ total_price 且 qty > 1 时提出纠错
- 仅返回 JSON，不要解释
"""


def _build_rows_text(rows: list[dict]) -> str:
    """Build a compact text table of quote rows for the LLM prompt."""
    lines = []
    for r in rows[:60]:  # Cap at 60 rows for token budget
        line = (
            f"[id={r.get('quote_id')}] "
            f"供应商={r.get('supplier_name','')} | "
            f"名称={r.get('material_name','')} | "
            f"规格={r.get('spec','')} | "
            f"单位={r.get('unit','')} | "
            f"数量={r.get('quantity','')} | "
            f"单价={r.get('unit_price','')} | "
            f"合价={r.get('total_price','')}"
        )
        lines.append(line)
    text = "\n".join(lines)
    if len(rows) > 60:
        text += f"\n...（共 {len(rows)} 行，仅展示前 60 行）"
    return text


def suggest_alignment(
    rows: list[dict],
    category: str,
    supplier_names: list[str],
    client: OpenAI,
    model: str = "qwen-plus",
    timeout: int = 120,
) -> dict[str, Any]:
    """Call LLM to suggest alignment groups and field corrections.

    Returns:
        {"groups": [...], "field_fixes": [...], "tokens_used": int, "duration_ms": int}
        On failure: {"groups": [], "field_fixes": [], "error": str}
    """
    try:
        rows_text = _build_rows_text(rows)
        prompt = BID_ALIGNMENT_PROMPT.format(
            category=category,
            supplier_names="、".join(supplier_names),
            rows_text=rows_text,
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

        # Parse JSON
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

        groups = data.get("groups", [])
        fixes = data.get("field_fixes", [])

        # Validate group structure
        valid_groups = []
        for g in groups:
            if isinstance(g, dict) and "suggested_name" in g and "items" in g:
                valid_groups.append({
                    "suggested_name": str(g.get("suggested_name", "")),
                    "suggested_spec": str(g.get("suggested_spec", "")),
                    "confidence": float(g.get("confidence", 0)),
                    "reason": str(g.get("reason", "")),
                    "items": g.get("items", []),
                })

        valid_fixes = []
        for f in fixes:
            if isinstance(f, dict) and "quote_id" in f:
                valid_fixes.append({
                    "quote_id": int(f["quote_id"]),
                    "field": str(f.get("field", "unit_price")),
                    "current": f.get("current"),
                    "suggested": f.get("suggested"),
                    "confidence": float(f.get("confidence", 0)),
                    "reason": str(f.get("reason", "")),
                })

        return {
            "groups": valid_groups,
            "field_fixes": valid_fixes,
            "tokens_used": tokens,
            "duration_ms": duration_ms,
        }

    except Exception as e:
        log.warning("bid_alignment LLM call failed: %s", e)
        return {
            "groups": [],
            "field_fixes": [],
            "error": str(e),
        }
