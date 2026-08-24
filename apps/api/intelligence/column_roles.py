"""表头列 → 规范角色：**共享词表 + 确定性验证 + 模型兜底**（docs/design/40）。

采购清单和报价清单出自不同的人、不同的模板，列名几乎没有一次是一样的。真实语料
里光"名称"这一个角色就有 `名称` / `品名` / `项目名称` / `材料/设备名称` 四种写法，
而徐汇的采购清单**根本没有规格列**（规格串直接写在名称列里）。靠往关键词表里
不断加同义词，下一份写 `除税单价` 或 `金额（不含税）` 的表还是会掉下去。

## 三层结构，顺序不可交换

1. **词表**（`propose_by_keywords`）——确定性、零成本、对已知形状完全正确。
   永远先跑。已知语料因此一次模型都不调，既有测试保持离线可复现。
2. **验证**（`verify_roles`）——**这一层是安全内核**。它不问映射是谁给的，只问
   证据答不答应：数量/价格列能不能解析成数、`数量×单价≈合价` 闭不闭合、
   `合价×(1+税率)≈价税合计` 对不对得上、名称列是不是文本。
3. **模型**（`propose_by_llm`）——只在第 2 步判词表不及格时才调用，产出再过一次
   第 2 步。**没过验证的映射一律不用。**

第 2 层在第 3 层之前存在，是这套设计成立的前提。CLAUDE.md 说「LLM 只解释确定性
结果」，指的是不许它改评标事实、不许它重排候选。判"哪列是数量"不是评标事实，
而且判错会被算术当场抓住——它跟"让模型决定哪行对哪行"是完全不同的风险等级：
行对齐的错误没有独立证据可以证伪，列映射的错误有。

**行对齐不走这条路。** 数量序列已经能确定性解到 100%（design/39），那里没有模型
的位置。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Sequence

from apps.api.core.domain_config import (
    COLUMN_ROLE_ARITHMETIC_MIN_RATE,
    COLUMN_ROLE_NUMERIC_MIN_RATE,
    COLUMN_ROLE_TEXT_MIN_RATE,
)
from apps.api.core.utils import parse_num, parse_rate

log = logging.getLogger(__name__)

TextCall = Callable[[str], str]

# 规范角色词表。**这是唯一的真值来源**：词表匹配、模型提示词、验证器三处都从
# 这里取，不许任何一处自己另写一份角色名。
ROLE_LABELS: dict[str, str] = {
    "seq": "序号",
    "name": "材料或设备名称",
    "spec": "规格",
    "model": "型号",
    "brand": "品牌",
    "material_type": "材质",
    "unit": "计量单位",
    "quantity": "数量",
    "unit_price": "单价（原文未标明含税/不含税）",
    "unit_price_incl_tax": "含税单价",
    "unit_price_excl_tax": "不含税单价",
    "total_price": "合价（原文未标明含税/不含税）",
    "total_price_incl_tax": "含税合价 / 价税合计",
    "total_price_excl_tax": "不含税合价",
    "tax_rate": "税率",
    "tax_amount": "税额",
    "supplier": "供应商 / 投标单位",
    "remark": "备注",
}

# 数值型角色。验证器要求这些列的取值绝大多数能解析成数。
_NUMERIC_ROLES = frozenset({
    "quantity", "unit_price", "unit_price_incl_tax", "unit_price_excl_tax",
    "total_price", "total_price_incl_tax", "total_price_excl_tax",
    "tax_rate", "tax_amount",
})

# 缺了就没法当一份清单用的角色。
_REQUIRED_ROLES = ("name", "quantity")

# 单价/合价的税基配对。与 `draft_integrity._PRICE_PAIRS` 同一口径：**绝不跨组拼**。
_PRICE_PAIRS: tuple[tuple[str, str], ...] = (
    ("unit_price_excl_tax", "total_price_excl_tax"),
    ("unit_price_incl_tax", "total_price_incl_tax"),
    ("unit_price", "total_price"),
)


class VerifyResult:
    """验证结论。`ok=False` 时 `reasons` 说明是哪条证据不答应——这些话会进日志和
    审计，必须是人看得懂的事实陈述，不是检查项代号。"""

    def __init__(self, ok: bool, reasons: list[str], evidence: dict):
        self.ok, self.reasons, self.evidence = ok, reasons, evidence

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"VerifyResult(ok={self.ok}, reasons={self.reasons})"


def _column_values(rows: Sequence[Sequence], idx: int) -> list[str]:
    out = []
    for r in rows:
        if idx < len(r):
            v = r[idx]
            out.append("" if v is None else str(v).strip())
    return out


def _numeric_rate(values: Sequence[str], *, rate: bool = False) -> float | None:
    """非空取值里能解析成数的占比；**整列为空时返回 None = 没有证据**。

    两处都不能想当然：

    - **空格不计入分母。** 一列有一半没填，不代表另一半读错了；那是数据稀疏。
    - **整列空不是"0% 能解析"。** 采购清单的价格列**按定义**就是空的——空白表
      正是它的判据（design/28 §2），投标方才往里填。把空列判成"认错列"会让
      每一份合格的采购清单都验不过，然后被推去问模型，而模型也答不出一个
      不存在的列。没有证据就是没有证据，不能按最差处理。
    """
    filled = [v for v in values if v]
    if not filled:
        return None
    fn = parse_rate if rate else parse_num
    return sum(1 for v in filled if fn(v) is not None) / len(filled)


def _text_rate(values: Sequence[str]) -> float | None:
    """非空取值里"不是纯数字"的占比。名称列整列是数字，说明认错了列。
    整列为空同样返回 None——名称列真空了会由"必填角色"那条挡下，不在这里重复判。
    """
    filled = [v for v in values if v]
    if not filled:
        return None
    return sum(1 for v in filled if parse_num(v) is None) / len(filled)


def verify_roles(roles: dict[str, int], rows: Sequence[Sequence]) -> VerifyResult:
    """**安全内核**：这份 {角色: 列下标} 映射，数据本身答不答应？

    只用证据，不用词表——所以它对"映射是词表给的还是模型给的"完全中立，
    这正是它能当兜底闸门的原因。

    四类证据：
      必填     缺 name/quantity → 这就不是一份可用的清单；
      数值型   数量/价格/税率列，非空取值里能解析成数的占比要够高；
      文本型   名称列不能整列是数字；
      算术     **同税基**配对下 `数量×单价≈合价` 的闭合率要够高。

    算术是这里最强的一条：合价列认错、单价跨税基配错，闭合率立刻塌。它也是
    模型提议敢被采纳的主要理由——猜错当场露馅，不会落库。

    ## 它抓不到什么：数量 ↔ 单价 对调

    **乘法可交换。** `数量×单价` 与 `单价×数量` 逐位完全相同，所以这两列互换之后
    算术闭合率一点不变，类型判据也看不出来（两列都是数字）。这条局限是真的，
    不拿"大概不会发生"糊过去。

    单文件层面没有独立证据能证伪它：数量不总是整数（实测 1905.25、2882.94），
    金额也不总是两位小数。真正能证伪它的证据是**跨供应商**的——同一条目各家的
    数量必须相同、单价必须不同（`test_quantities_are_identical_across_suppliers`
    断言的正是这条），但那要等到对齐阶段才拿得到，解析时没有。

    所以风险在**接线层**收窄，而不是在这里假装能验：`tabular_ingestion.resolve_columns`
    只在词表对某个角色**根本没有意见**时才让模型填那个格子。词表能凭列名认出
    `数量` 的表，模型改不动它；词表认不出 `数量` 的表，本来也没有第二个意见
    可以对照。剩余风险随 `column_source="llm"` 落进 `_doc_meta`，可审计。
    """
    reasons: list[str] = []
    evidence: dict = {}

    for role in _REQUIRED_ROLES:
        if roles.get(role) is None:
            reasons.append(f"没有认出「{ROLE_LABELS[role]}」列")

    for role, idx in roles.items():
        if idx is None or role not in _NUMERIC_ROLES:
            continue
        r = _numeric_rate(_column_values(rows, idx), rate=(role == "tax_rate"))
        if r is None:
            evidence[f"numeric:{role}"] = "empty"
            continue
        evidence[f"numeric:{role}"] = round(r, 3)
        if r < COLUMN_ROLE_NUMERIC_MIN_RATE:
            reasons.append(
                f"「{ROLE_LABELS[role]}」列只有 {r:.0%} 的取值能当成数读——认错列了")

    if roles.get("name") is not None:
        t = _text_rate(_column_values(rows, roles["name"]))
        if t is None:
            reasons.append(f"「{ROLE_LABELS['name']}」列整列为空")
        else:
            evidence["text:name"] = round(t, 3)
            if t < COLUMN_ROLE_TEXT_MIN_RATE:
                reasons.append(
                    f"「{ROLE_LABELS['name']}」列有 {1 - t:.0%} 的取值是纯数字"
                    "——像是认成了序号或数量")

    qi = roles.get("quantity")
    for up, tp in _PRICE_PAIRS:
        ui, ti = roles.get(up), roles.get(tp)
        if qi is None or ui is None or ti is None:
            continue
        qs = _column_values(rows, qi)
        us = _column_values(rows, ui)
        ts = _column_values(rows, ti)
        hit = total = 0
        for q, u, t in zip(qs, us, ts):
            qn, un, tn = parse_num(q), parse_num(u), parse_num(t)
            if not qn or not un or tn is None:
                continue
            total += 1
            denom = max(abs(tn), abs(qn * un))
            if denom and abs(tn - qn * un) / denom <= 0.01:
                hit += 1
        if total:
            r = hit / total
            evidence[f"arith:{up}|{tp}"] = round(r, 3)
            if r < COLUMN_ROLE_ARITHMETIC_MIN_RATE:
                reasons.append(
                    f"「{ROLE_LABELS[up]}」×「{ROLE_LABELS['quantity']}」只有 {r:.0%} "
                    f"的行等于「{ROLE_LABELS[tp]}」——这三列至少有一列认错了")
        break   # 只验第一组两端齐全的税基，跟 draft_integrity 同一约定

    return VerifyResult(not reasons, reasons, evidence)


# ── 模型提议 ────────────────────────────────────────────────────────────────

_PROMPT = """You are given the header row and a few data rows of one table from a
construction procurement or bid-quotation spreadsheet. Assign each column to one
role, or to "ignore" when no role fits.

Roles and what they mean:
{roles}

What matters here: the same role is written many different ways across suppliers,
and some tables omit a column entirely (a spec string may live inside the name
column). Decide from the data, not only from the header wording.

Tax basis is significant. A column labelled as excluding tax and one labelled as
including tax are different roles; never map both to the generic role. Use the
generic `unit_price` / `total_price` roles only when the table does not
distinguish tax basis at all.

Example (fictional):
  header: ["No.", "Item", "Model", "Unit", "Qty", "Rate ex-VAT", "Amount ex-VAT", "VAT %", "Amount incl."]
  answer: {{"seq":0,"name":1,"model":2,"unit":3,"quantity":4,"unit_price_excl_tax":5,"total_price_excl_tax":6,"tax_rate":7,"total_price_incl_tax":8}}

Reply with JSON only: an object mapping role name to zero-based column index.
Omit roles that are not present. Do not invent columns.

header: {header}
rows:
{sample}
"""


def propose_by_llm(
    header: Sequence[str], rows: Sequence[Sequence], call: TextCall,
    *, sample_rows: int = 5,
) -> dict[str, int] | None:
    """问模型要一份 {角色: 列下标}。失败返回 None，**绝不抛异常打断解析**。

    返回值未经验证——调用方必须再过一次 `verify_roles`。这里不合并这两步，
    是为了让"提议"和"采纳"在代码里也分得清清楚楚：任何新的提议来源都得走
    同一道闸。
    """
    try:
        roles_doc = "\n".join(f"  {k}: {v}" for k, v in ROLE_LABELS.items())
        sample = "\n".join(
            json.dumps([("" if c is None else str(c)) for c in r], ensure_ascii=False)
            for r in rows[:sample_rows])
        raw = call(_PROMPT.format(
            roles=roles_doc,
            header=json.dumps([str(h or "") for h in header], ensure_ascii=False),
            sample=sample))
        return _parse_llm_roles(raw, len(header))
    except Exception:                                              # noqa: BLE001
        log.warning("列角色模型提议失败，回退词表结果", exc_info=True)
        return None


_PROMPT_LAYOUT = """You are given the first rows of one sheet from a construction
procurement list. Find which row is the header row, and assign columns to roles.

Roles and what they mean:
{roles}

The sheet usually opens with one or more title lines (project name, document
title) before the real header row. A procurement list often carries no prices at
all — the bidder fills those in — so absent price columns are normal, not an
error. Some sheets have no separate spec column and put the spec string inside
the name column; in that case map only `name`.

Example (fictional):
  rows:
    ["Riverside Depot Phase II - Materials Schedule", "", "", ""]
    ["No.", "Description", "Unit", "Qty"]
    ["1", "Cable XY-3x50", "m", "120.5"]
  answer: {{"header_row": 1, "roles": {{"seq":0,"name":1,"unit":2,"quantity":3}}}}

Reply with JSON only, of the form {{"header_row": <int>, "roles": {{...}}}}.
Column indices are zero-based within the header row.

rows:
{sample}
"""


def propose_layout_by_llm(
    rows: Sequence[Sequence], call: TextCall, *, scan_rows: int = 12,
) -> tuple[int, dict[str, int]] | None:
    """连**表头在第几行**一起问。返回 `(header_row, roles)`；失败返回 None。

    招标侧比报价侧多一道坎：`_find_header_row` 要靠"同时出现序号类和名称类关键词"
    才能定位表头行，表头一旦写成词表不认识的字，连表头在哪都找不到——这时候
    只给列角色是没用的，得先知道从第几行开始才是数据。

    返回值同样**未经验证**，调用方必须自己再判一次。
    """
    try:
        sample = "\n".join(
            json.dumps([("" if c is None else str(c)) for c in r], ensure_ascii=False)
            for r in rows[:scan_rows])
        raw = call(_PROMPT_LAYOUT.format(
            roles="\n".join(f"  {k}: {v}" for k, v in ROLE_LABELS.items()),
            sample=sample))
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        hi = data.get("header_row")
        if not isinstance(hi, bool) and isinstance(hi, int) and 0 <= hi < min(scan_rows, len(rows)):
            roles = _parse_llm_roles(json.dumps(data.get("roles") or {}), len(rows[hi]))
            if roles:
                return hi, roles
        return None
    except Exception:                                              # noqa: BLE001
        log.warning("表格版式模型提议失败，回退词表结果", exc_info=True)
        return None


def _parse_llm_roles(raw: str, n_cols: int) -> dict[str, int] | None:
    """从模型回复里抠出 JSON 并做**结构性净化**。

    净化不是防御性编程的仪式，是必须的：越界下标会让解析崩在一个跟列映射毫无
    关系的地方，未知角色名会静默污染下游字段，一个下标被两个角色认领会让
    `_get_cell` 读出互相矛盾的值。宁可丢掉可疑项也不让它进管线。
    """
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, int] = {}
    claimed: set[int] = set()
    for role, idx in data.items():
        if role not in ROLE_LABELS or not isinstance(idx, int):
            continue
        if not (0 <= idx < n_cols) or idx in claimed:
            continue
        out[role] = idx
        claimed.add(idx)
    return out or None
