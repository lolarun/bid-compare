"""Cross-cutting utilities — no business logic, no DB access."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

_KNOWN_SEPARATORS_RE = re.compile(r"[,，¥￥$\s　]")
_NON_NUMERIC_RE = re.compile(r"[^\d.\-]")


def parse_num(v: Any, *, lenient: bool = False) -> float | None:
    """Parse a number out of OCR/user text. None / '' / unparseable → None.

    评审 N7：此前 vl_direct._num、price_basis._num、pipeline._coerce_num、
    quote_fact._coerce_num 是四份各自独立的实现，同一个值在一层能解析、在
    下一层失败。收拢到这一处，行为差异用 lenient 参数表达而不是靠四份互相
    看不见彼此的拷贝各自决定：

    - lenient=False（默认，原 vl_direct/price_basis 的行为）：只剥离已知的
      分隔符/货币符号（,，¥￥$ 及各种空白），剩下的字符若不能直接 float()
      就返回 None——不猜测，"约1500元"这类夹杂文字的输入按无法解析处理。
    - lenient=True（原 pipeline._coerce_num/quote_fact._coerce_num 的行为）：
      剥离一切非数字/小数点/负号字符后再解析，能从自由文本里挖出数字
      （"约1500元/米" → 1500.0），代价是可能把结构性错误的输入读出一个
      似是而非的数——只在调用方已经决定"尽力抠出数字"可以接受时使用
      （见各调用点原有的选型说明）。
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _KNOWN_SEPARATORS_RE.sub("", str(v)).strip()
    if lenient:
        s = _NON_NUMERIC_RE.sub("", s)
    if not s or s in {".", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_rate(v: Any) -> float | None:
    """把税率读成小数。`"13%"` → 0.13；`"13"` → 0.13；`0.13` → 0.13。

    `parse_num` 只剥离已知分隔符，不认百分号，`float("13%")` 直接 ValueError。
    此前 `paddle_vl._parse_rate` 各自实现了一份，且它的注释明确留了个问题：
    「vl_quote.build_quote_fields 那层理论上有同样的问题，这里不代它下结论」。
    收拢到这里，让所有读税率的入口用同一把尺子。

    **裸数字按百分数处理。** 增值税率的真实取值只有 0/1/3/5/6/9/13(%) 这几档，
    没有一档会写成大于 1 的小数；反过来 Excel 里把税率存成 `13` 而显示成
    `13%` 是常态。故 `>1` 一律除以 100，`<=1` 原样当小数——这条判据对真实税率
    全域无歧义，不是猜测。
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    if s.endswith("%"):
        n = parse_num(s[:-1])
        return None if n is None else n / 100
    n = parse_num(s)
    if n is None:
        return None
    return n / 100 if n > 1 else n


def parse_id_csv(value: str, field_name: str = "ids") -> list[int]:
    """Parse a comma-separated integer string into list[int].

    Raises HTTP 400 with a descriptive message on parse failure.
    Returns an empty list when value is empty or whitespace-only.
    """
    if not value or not value.strip():
        return []
    try:
        return [int(x) for x in value.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, f"{field_name} 须为逗号分隔的整数")
