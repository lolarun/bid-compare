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
