"""从备注自由文本抽份级口径候选 —— P1（用户 2026-09-03 决策 2：直接上 LLM）。

设计见 `.claude/plans/comparability-basis-dimensions.md` D4。**这里只产候选，
不产事实**：输出一律以 `extracted` 状态落库，要人确认过才叫 `confirmed`，
而门禁（`services/matrix/basis_consistency.py`）只吃 `confirmed`。模型抽错时，
错误值不会自己变成"这一轮可以比"的依据。

## 三态，不用 null 兜底

用户决策 2 的附加约束：必须能分辨

- `extracted`         —— 抽到了候选值
- `not_present`       —— **原文里确实没有**这个维度的声明（业务事实，本身是疑点）
- `extraction_failed` —— 模型报错/超时/输出不可解析（要重试或人工补）

后两者混成一个空值，就永远查不出模型漏抽了多少。所以模型被要求为每个维度显式
返回 `present: true/false`，而不是"没抽到就不出现在 JSON 里"——键缺失一律当失败
处理，不当"没有"。

## 归一表是可审计的词表，不是模型即兴判断

「不含安装 / 不含安装费 / 安装另计」要归到同一个值，这件事由下面的
`SCOPE_VOCAB` 决定，不是让模型自由发挥——模型只负责挑出**原文片段**并给出它
认为匹配的槽位，归一在代码里做。词表改动可 diff、可回滚。

模型给出词表以外的说法时**不硬塞**：`scope` 落 `other` 并原样保留 raw_text，
留给人确认——硬套到最近的词条上会把"含运费不含安装"悄悄变成"不含安装"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from apps.api.models.submission_basis import (
    DIM_COMMODITY_BENCHMARK,
    DIM_DELIVERY_SCOPE,
    DIM_PAYMENT_TERMS,
    STATUS_EXTRACTED,
    STATUS_EXTRACTION_FAILED,
    STATUS_NOT_PRESENT,
)

log = logging.getLogger(__name__)

# ── 归一词表（可审计）───────────────────────────────────────────────────────
#: 交付范围。键是模型可能给出的说法，值是归一后的槽位。
SCOPE_VOCAB: dict[str, str] = {
    "含安装": "incl_installation",
    "包含安装": "incl_installation",
    "含安装费": "incl_installation",
    "不含安装": "excl_installation",
    "不含安装费": "excl_installation",
    "安装另计": "excl_installation",
    "不含安装及调试": "excl_installation",
}
SCOPE_OTHER = "other"


@dataclass
class BasisCandidate:
    """一个维度的抽取结果。`status` 决定它能不能进一步被确认。"""

    dim: str
    status: str
    value: dict | None = None
    raw_text: str = ""
    source_ref: dict | None = None


def _normalize_scope(said: str) -> tuple[str, bool]:
    """把模型说法归一到槽位。返回 (槽位, 是否命中词表)。

    没命中不猜最近词条——「含运费不含安装」硬套成「不含安装」会丢掉运费口径。
    """
    key = (said or "").strip()
    if key in SCOPE_VOCAB:
        return SCOPE_VOCAB[key], True
    return SCOPE_OTHER, False


def _coerce_float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


#: 生产提示词一律用**虚构**示例（CLAUDE.md §4：不得出现真实供应商/项目/文件名）。
PROMPT = """你在读一份投标报价文件里的备注与商务条款文字。

任务：判断下面三个口径维度，原文里**有没有**明确声明；有就摘出原文片段。

1. delivery_scope 交付范围——报价是否包含安装（或明确不含安装、安装另计）
2. commodity_benchmark 原材料价格基准——报价是否以某种原材料的基准价计价
   （例如"某金属基准价 XXXXX 元/吨"）
3. payment_terms 付款条件——付款比例、账期、质保金比例与年限、票据类型

严格要求：

- 只做摘取，不做推断。原文没说就是没说，**不要**根据行业惯例补全。
- 每个维度都必须出现在结果里，用 present 明确表态：
  present=true 时给 quote（原文片段，逐字照抄，不改写）；
  present=false 时 quote 留空字符串。
- 不要计算、不要换算、不要比较优劣、不要给建议。

只输出 JSON，形如（这是**格式示例**，值是虚构的，不要照抄内容）：

{
  "delivery_scope":      {"present": true,  "quote": "报价不含安装", "said": "不含安装"},
  "commodity_benchmark": {"present": true,  "quote": "以某金属基准价 60000 元/吨计价",
                          "material": "某金属", "price": 60000, "unit": "元/吨"},
  "payment_terms":       {"present": false, "quote": ""}
}

待读文字：
---
{text}
---
"""


def extract_basis_from_text(
    text: str,
    *,
    client: Any,
    model: str,
    timeout: int = 60,
    source_ref: dict | None = None,
) -> list[BasisCandidate]:
    """从一份报价的备注/商务条款文字里抽三个维度的候选。

    `client` 是 OpenAI 兼容客户端（与 supplier_fill_llm.call_llm 同一套约定）。
    任何异常都收敛成三个 `extraction_failed` 候选——**不抛给调用方**：一份文件
    抽不出来不该让整轮入库失败，但也绝不能静悄悄当成"没有声明"。
    """
    dims = (DIM_DELIVERY_SCOPE, DIM_COMMODITY_BENCHMARK, DIM_PAYMENT_TERMS)

    if not (text or "").strip():
        # 没有原文可读 ≠ 抽取失败，也 ≠ 投标方没声明——它是"我们手上没有这段文字"。
        # 归到 failed：需要有人去补原文，而不是被当成"没有声明"放过。
        return [
            BasisCandidate(dim=d, status=STATUS_EXTRACTION_FAILED, source_ref=source_ref)
            for d in dims
        ]

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROMPT.replace("{text}", text)}],
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — 任何失败都收敛成 failed 状态
        log.warning("basis extraction failed: %s", exc)
        return [
            BasisCandidate(dim=d, status=STATUS_EXTRACTION_FAILED, source_ref=source_ref)
            for d in dims
        ]

    return [_candidate_for(d, data.get(d), source_ref) for d in dims]


def _candidate_for(dim: str, node: Any, source_ref: dict | None) -> BasisCandidate:
    # 键缺失/形状不对 → 失败，**不当成"没有声明"**。模型漏答和投标方没写是两回事。
    if not isinstance(node, dict) or "present" not in node:
        return BasisCandidate(dim=dim, status=STATUS_EXTRACTION_FAILED, source_ref=source_ref)

    quote = str(node.get("quote") or "")

    if not node.get("present"):
        return BasisCandidate(
            dim=dim, status=STATUS_NOT_PRESENT, raw_text=quote, source_ref=source_ref,
        )

    if dim == DIM_DELIVERY_SCOPE:
        slot, hit = _normalize_scope(str(node.get("said") or quote))
        return BasisCandidate(
            dim=dim, status=STATUS_EXTRACTED,
            # 没命中词表就落 other 并保留原文，留给人判——不硬套最近词条
            value={"scope": slot, "vocab_hit": hit},
            raw_text=quote, source_ref=source_ref,
        )

    if dim == DIM_COMMODITY_BENCHMARK:
        price = _coerce_float(node.get("price"))
        material = str(node.get("material") or "").strip()
        if price is None or not material:
            # 说有基准却给不出料/价 → 失败，不落一个半截的值
            return BasisCandidate(
                dim=dim, status=STATUS_EXTRACTION_FAILED, raw_text=quote, source_ref=source_ref,
            )
        return BasisCandidate(
            dim=dim, status=STATUS_EXTRACTED,
            value={"material": material, "price": price,
                   "unit": str(node.get("unit") or "").strip()},
            raw_text=quote, source_ref=source_ref,
        )

    # payment_terms：**不解析成数值槽位**。真实材料里是"货到现场二个月后支付至
    # 送货金额的70%；竣工验收合格后再支付15%…"这种长句，拆成 advance_pct 一类的
    # 数字会丢掉条件（"竣工验收合格后"）。这一版按原文整体比对：同一轮里两家
    # 条款文字不同就是不同，由人去看差在哪。
    return BasisCandidate(
        dim=dim, status=STATUS_EXTRACTED,
        value={"terms_text": quote},
        raw_text=quote, source_ref=source_ref,
    )
