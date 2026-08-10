"""draft_integrity.py — 入库前的两道结构门：列错位 与 重复行。

两类缺陷的共同点是**下游察觉不到**：错位之后的金额仍然是"合法的数字"，重复行
仍然逐行通过算术校验（数量×单价=合价），既有的算术门、派生金额门、declared_total
门全部放行。只有在数据还是"表"的时候用结构判据才拦得住。

实测形态（2026-08-09 七份 VL 直出）：

- **右移**：表头把「规格/型号」并成一列，数据仍是两列 → 第 5 列往后整体右移一位，
  90 行里 86 行受影响。数量列读到"个"、单价列读到"1"、价税合计列读到税额。
- **左移**：某类目行的规格串占了规格列、真实规格丢失 → 数量→单价、单价→合价整体
  左移，43 行的数量为空。这些行的"单价"其实是数量、"合价"其实是单价。
- **重复**：215 明细行对实际 136 行，47 个 (规格,数量) 键各出现两次，金额虚增 42%。
  同一份文件里同一批页被读了多遍。

两道门都遵守 CLAUDE.md §4「标注而非丢弃、保留原值与依据」：
**只标注、只阻断，绝不删除行、不改写值、不猜测正确的列序。**
恢复正确值需要回到原始页面重读，那不是本模块的职责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.api.core.domain_config import (
    INTEGRITY_ARITHMETIC_TOLERANCE,
    INTEGRITY_COLUMN_SHIFT_BLOCKED_COUNT,
    INTEGRITY_COLUMN_SHIFT_BLOCKED_RATIO,
    INTEGRITY_DUPLICATE_BLOCKED_AMOUNT_RATIO,
    INTEGRITY_MULTIPLIER_TOLERANCE,
    INTEGRITY_TRUNCATION_MIN_SAMPLES,
    INTEGRITY_TRUNCATION_MIN_SUSPECTS,
    MATCH_ARITHMETIC_MAX_ERROR_RATE,
    MATCH_ARITHMETIC_VAT_TOLERANCE,
)

# 结论三态与 CLAUDE.md §4 的质量分级一致
OK = "ok"
REVIEW = "review"
BLOCKED = "blocked"

COLUMN_SHIFT_FLAG = "column_shift"
DUPLICATE_FLAG = "duplicate_row"
ARITHMETIC_FLAG = "arithmetic_mismatch"
TRUNCATION_FLAG = "value_truncated"


def _worst(*verdicts: str) -> str:
    for v in (BLOCKED, REVIEW):
        if v in verdicts:
            return v
    return OK


# ─── ① 列错位 ────────────────────────────────────────────────────────────────

@dataclass
class RowShape:
    """一行的形状异常。row_index 是 0-based 数据行序（不含表头）。"""
    row_index: int
    cells: int
    expected: int
    kind: str                    # extra_cells | missing_cells
    preview: str = ""

    def to_dict(self) -> dict:
        return {"row_index": self.row_index, "cells": self.cells,
                "expected": self.expected, "kind": self.kind,
                "preview": self.preview, "reason": (
                    "数据列数多于表头，按列名取值整体错位"
                    if self.kind == "extra_cells" else "数据列数少于表头，尾列缺失")}


@dataclass
class ColumnAlignmentReport:
    header: list[str] = field(default_factory=list)
    total_rows: int = 0
    bad_rows: list[RowShape] = field(default_factory=list)
    verdict: str = OK

    @property
    def extra_rows(self) -> list[RowShape]:
        return [r for r in self.bad_rows if r.kind == "extra_cells"]

    @property
    def missing_rows(self) -> list[RowShape]:
        return [r for r in self.bad_rows if r.kind == "missing_cells"]

    @property
    def bad_row_indices(self) -> set[int]:
        return {r.row_index for r in self.bad_rows}

    def to_dict(self) -> dict:
        return {"header_len": len(self.header), "total_rows": self.total_rows,
                "extra_cell_rows": len(self.extra_rows),
                "missing_cell_rows": len(self.missing_rows),
                "verdict": self.verdict,
                "rows": [r.to_dict() for r in self.bad_rows[:50]]}


def check_column_alignment(header: list[str], rows: list[list],
                           *, ignore_trailing_empty: bool = True) -> ColumnAlignmentReport:
    """表头列数 vs 每行单元格数。

    单元格**多于**表头 = 整表右移，该行所有按列名取的值都不可信 → 逐行 BLOCKED。
    单元格**少于**表头 = 尾列缺失，通常只影响备注类字段 → 逐行 REVIEW。
    尾部空串按缺列还是按正常处理由 ignore_trailing_empty 决定：CSV 写出时常给
    尾列补空串，那不是缺陷。

    整份的结论按占比/绝对数升级（阈值见 domain_config），避免一行异常牵连整份，
    也避免大面积错位被小占比洗白。
    """
    expected = len(header)
    report = ColumnAlignmentReport(header=list(header), total_rows=len(rows))
    if expected == 0:
        report.verdict = BLOCKED
        return report

    for i, row in enumerate(rows):
        cells = list(row)
        if ignore_trailing_empty:
            while len(cells) > expected and (cells[-1] is None or str(cells[-1]).strip() == ""):
                cells.pop()
        n = len(cells)
        if n == expected:
            continue
        kind = "extra_cells" if n > expected else "missing_cells"
        preview = ",".join("" if c is None else str(c) for c in cells)[:120]
        report.bad_rows.append(RowShape(row_index=i, cells=n, expected=expected,
                                        kind=kind, preview=preview))

    extra = len(report.extra_rows)
    if extra:
        widespread = (extra >= INTEGRITY_COLUMN_SHIFT_BLOCKED_COUNT
                      or extra / max(len(rows), 1) > INTEGRITY_COLUMN_SHIFT_BLOCKED_RATIO)
        report.verdict = BLOCKED if widespread else REVIEW
    elif report.missing_rows:
        report.verdict = REVIEW
    return report


# ─── ② 重复行 ────────────────────────────────────────────────────────────────

_SPEC_NOISE = re.compile(r"[\s　]")


def normalize_key_text(s) -> str:
    """规格/名称归一：去空白、统一乘号与大小写。只用于比对，不写回原值。"""
    t = str(s or "").upper()
    t = _SPEC_NOISE.sub("", t)
    return t.replace("×", "*").replace("X", "*")


def _num(x):
    if x is None or x == "":
        return None
    try:
        return round(float(str(x).replace(",", "")), 4)
    except (TypeError, ValueError):
        return None


@dataclass
class DuplicateGroup:
    """同一 (名称,规格,数量,单价) 三元组以上完全一致的一组行。"""
    key: tuple
    row_indices: list[int]
    amount: float = 0.0          # 该组**重复部分**的金额（保留第一行，其余计入）

    def to_dict(self) -> dict:
        name, spec, qty, price = self.key
        return {"name": name, "spec": spec, "qty": qty, "unit_price": price,
                "row_indices": self.row_indices, "occurrences": len(self.row_indices),
                "duplicate_amount": round(self.amount, 2),
                "reason": "同名称/规格/数量/单价的行重复出现；可能是同一批页被读了多遍"}


@dataclass
class DuplicateReport:
    groups: list[DuplicateGroup] = field(default_factory=list)
    duplicate_amount: float = 0.0
    total_amount: float = 0.0
    verdict: str = OK

    @property
    def duplicate_row_indices(self) -> set[int]:
        """重复组里**除第一行以外**的行序：第一行是正常行，不该被牵连。"""
        return {i for g in self.groups for i in g.row_indices[1:]}

    @property
    def amount_ratio(self) -> float:
        return (self.duplicate_amount / self.total_amount) if self.total_amount else 0.0

    def to_dict(self) -> dict:
        return {"group_count": len(self.groups),
                "duplicate_rows": len(self.duplicate_row_indices),
                "duplicate_amount": round(self.duplicate_amount, 2),
                "total_amount": round(self.total_amount, 2),
                "amount_ratio": round(self.amount_ratio, 4),
                "verdict": self.verdict,
                "groups": [g.to_dict() for g in self.groups[:50]]}


def find_duplicate_rows(items: list[dict], *,
                        name_key: str = "material", spec_key: str = "spec",
                        qty_key: str = "qty", price_key: str = "unit_price",
                        total_key: str = "total_price") -> DuplicateReport:
    """按 (名称, 规格, 数量, 单价) 四元组检重。

    为什么用四元组而不是 (规格,数量)：同一份清单里同料同量不同价是合法的（不同
    楼层/不同批次分两行报），只按规格+数量会把这些正常行判成重复。四个字段全等
    才有理由怀疑是同一行被读了两遍。

    **合法的重复确实存在**（同料同量同价分两行），所以本函数的产物是 REVIEW 材料，
    只有当重复金额占比越过阈值时才升级为 BLOCKED——那种规模不可能是真实重复。
    """
    report = DuplicateReport()
    buckets: dict[tuple, list[int]] = {}
    amounts: dict[int, float] = {}

    for i, it in enumerate(items):
        qty = _num(it.get(qty_key))
        price = _num(it.get(price_key))
        key = (normalize_key_text(it.get(name_key)), normalize_key_text(it.get(spec_key)),
               qty, price)
        if not key[0] and not key[1]:
            continue                      # 名称与规格都空：不是可比对的行
        buckets.setdefault(key, []).append(i)
        t = _num(it.get(total_key))
        if t is None and qty is not None and price is not None:
            t = round(qty * price, 4)     # 仅用于估算重复金额规模，不写回任何字段
        amounts[i] = t or 0.0

    report.total_amount = sum(amounts.values())
    for key, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        dup_amount = sum(amounts.get(i, 0.0) for i in idxs[1:])
        report.groups.append(DuplicateGroup(key=key, row_indices=idxs, amount=dup_amount))
    report.groups.sort(key=lambda g: -g.amount)
    report.duplicate_amount = sum(g.amount for g in report.groups)

    if report.groups:
        report.verdict = (BLOCKED
                          if report.amount_ratio > INTEGRITY_DUPLICATE_BLOCKED_AMOUNT_RATIO
                          else REVIEW)
    return report


# ─── ③ 行内算术闭合 ──────────────────────────────────────────────────────────
#
# 泛化要点（这一节最容易写成只对手头样本成立的东西）：
#
# 1. **税基必须成对**。单价与合价只能同税基相比：不含税单价 × 数量 = 不含税合价。
#    拿不含税单价去对含税合价，会把每一行都判成错，且偏差恰好是税率——看起来
#    像"系统性错误"，其实是比错了尺子。故按 (不含税, 含税, 无税分列) 三组成对
#    取值，取**第一组两端都在**的，绝不跨组拼。
# 2. **倍率是报价口径，不是错误**。实测同一批文档里，有的供应商按单根报价、有的
#    按双根合价报，合价/(数量×单价) 恒等于 2.0 或 0.5。这是报价方式的选择，
#    只能观测和标记，**禁止据此修正原值或反推数量**（HANDOFF §2 业务发现）。
# 3. **三缺一就不评估**。缺数量/单价/合价的行记为 not_evaluable，绝不当成通过——
#    把它们算作"通过"会让分母虚高、把真实错误稀释到阈值以下。
# 4. **税基不一致单独成一档**。偏差落在增值税量级时报 tax_basis_suspect，而不是
#    算术错误：两者的处置完全不同。

# 单价/合价的税基配对。顺序 = 优先级；每组两端都有值才使用，绝不跨组拼。
_PRICE_PAIRS: tuple[tuple[str, str], ...] = (
    ("unit_price_excl_tax", "total_price_excl_tax"),
    ("unit_price_incl_tax", "total_price_incl_tax"),
    ("unit_price", "total_price"),
)

# 常见的报价口径倍率。不是穷举，只是"看起来像口径选择而不是算错"的判据；
# 命中即标记交人工，不命中就按算术错误报——两条路都不会自动改值。
_PLAUSIBLE_MULTIPLIERS: tuple[float, ...] = (0.5, 2.0, 3.0, 1 / 3, 4.0, 0.25)


@dataclass
class ArithmeticResult:
    status: str                  # ok|mismatch|multiplier|tax_basis_suspect|not_evaluable
    basis: str = ""              # 实际用于比对的一对字段
    qty: float | None = None
    unit_price: float | None = None
    total_price: float | None = None
    computed: float | None = None
    deviation: float = 0.0       # |合价 − 数量×单价| / max(|合价|, |数量×单价|)
    implied_multiplier: float | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "basis": self.basis, "qty": self.qty,
                "unit_price": self.unit_price, "total_price": self.total_price,
                "computed": None if self.computed is None else round(self.computed, 4),
                "deviation": round(self.deviation, 6),
                "implied_multiplier": self.implied_multiplier, "reason": self.reason}


def arithmetic_deviation(qty: float, unit_price: float, total: float) -> float:
    """|合价 − 数量×单价| / max(|合价|, |数量×单价|)。

    分母取两者较大值而不是只取合价：合价被读成 0.01 这类情况下，用合价当分母会得到
    一个天文数字的偏差率，用较大值才能稳定地落在 [0,1] 区间里。
    """
    computed = qty * unit_price
    denom = max(abs(total), abs(computed))
    return abs(total - computed) / denom if denom else 0.0


def check_row_arithmetic(row, *, tolerance: float = INTEGRITY_ARITHMETIC_TOLERANCE,
                         qty_key: str = "qty") -> ArithmeticResult:
    """单行算术闭合。`row` 是任意支持 .get 的映射（items dict 或 DraftRow.fields）。"""
    qty = _num(row.get(qty_key))
    for up_key, tp_key in _PRICE_PAIRS:
        price, total = _num(row.get(up_key)), _num(row.get(tp_key))
        if price is None or total is None:
            continue
        if not qty or not price or not total:
            break                       # 该组齐全但含 0 → 无法评估，不再降级到下一组
        dev = arithmetic_deviation(qty, price, total)
        res = ArithmeticResult(status="ok", basis=f"{up_key}|{tp_key}", qty=qty,
                               unit_price=price, total_price=total,
                               computed=qty * price, deviation=dev)
        if dev <= tolerance:
            return res
        ratio = total / (qty * price)
        near = next((m for m in _PLAUSIBLE_MULTIPLIERS
                     if abs(ratio - m) <= INTEGRITY_MULTIPLIER_TOLERANCE * max(m, 1.0)), None)
        if near is not None:
            res.status, res.implied_multiplier = "multiplier", round(ratio, 4)
            res.reason = (f"合价/(数量×单价) ≈ {near:g}，像是报价口径（按根/按束/按套）"
                          f"而非算错；只记录，不修正")
            return res
        if dev <= MATCH_ARITHMETIC_VAT_TOLERANCE:
            res.status = "tax_basis_suspect"
            res.reason = "偏差落在增值税量级，疑似单价与合价不同税基"
            return res
        res.status = "mismatch"
        res.reason = "数量×单价与合价对不上，三者中至少一个读错"
        return res
    return ArithmeticResult(status="not_evaluable", qty=qty,
                            reason="缺数量/单价/合价之一，无法评估（不计为通过）")


@dataclass
class ArithmeticReport:
    results: list[ArithmeticResult] = field(default_factory=list)
    mismatch_indices: list[int] = field(default_factory=list)
    multiplier_indices: list[int] = field(default_factory=list)
    tax_suspect_indices: list[int] = field(default_factory=list)
    evaluable: int = 0
    verdict: str = OK

    @property
    def error_rate(self) -> float:
        return len(self.mismatch_indices) / self.evaluable if self.evaluable else 0.0

    def to_dict(self) -> dict:
        return {"evaluable": self.evaluable, "mismatch": len(self.mismatch_indices),
                "multiplier": len(self.multiplier_indices),
                "tax_basis_suspect": len(self.tax_suspect_indices),
                "error_rate": round(self.error_rate, 4), "verdict": self.verdict,
                "rows": [{"index": i, **self.results[i].to_dict()}
                         for i in self.mismatch_indices[:50]]}


def check_arithmetic(items: list[dict], **kw) -> ArithmeticReport:
    """整份的算术闭合。错误率越过 MATCH_ARITHMETIC_MAX_ERROR_RATE 升级为 BLOCKED。"""
    rep = ArithmeticReport(results=[check_row_arithmetic(it, **kw) for it in items])
    for i, r in enumerate(rep.results):
        if r.status == "not_evaluable":
            continue
        rep.evaluable += 1
        {"mismatch": rep.mismatch_indices, "multiplier": rep.multiplier_indices,
         "tax_basis_suspect": rep.tax_suspect_indices}.get(r.status, []).append(i)
    if rep.mismatch_indices:
        rep.verdict = (BLOCKED if rep.error_rate > MATCH_ARITHMETIC_MAX_ERROR_RATE
                       else REVIEW)
    elif rep.multiplier_indices or rep.tax_suspect_indices:
        rep.verdict = REVIEW
    return rep


# ─── ④ 数值截断 ──────────────────────────────────────────────────────────────
#
# 泛化要点：**不假定任何固定宽度、固定列名或固定小数位**。判据完全来自该列自身的
# 分布——某列存在硬宽度上限，且卡在上限的值小数位少于该列自己的常见小数位。
# 一份正常文档里数值宽度是连续分布的，卡在最大宽度的只有零星几个；被截断的列会在
# 上限处堆积一大片，且那一片恰好丢了小数。
#
# 三条统计护栏（数量、占比、疑似数下限）只是防止小样本瞎报，不含任何格式假设。
# 整数列自然不会命中：它的常见小数位是 0，没有值能"比 0 还少"。

_NUM_TEXT = re.compile(r"^[¥￥\s]*-?[\d,]+(?:\.\d*)?[\s]*$")


def _decimals(text: str) -> int:
    t = text.strip()
    return len(t.split(".", 1)[1]) if "." in t else 0


def _clean_num_text(text) -> str:
    return re.sub(r"[¥￥,\s]", "", str(text or ""))


@dataclass
class TruncationSuspect:
    row_index: int
    column: str
    value: str
    width: int
    decimals: int
    baseline_decimals: int
    corroborated: bool = False   # 算术残差恰好落在"少了小数位"的量级
    width_pileup: bool = False   # 该列在宽度上限处堆积（置信度信号，非判据）

    def to_dict(self) -> dict:
        return {"row_index": self.row_index, "column": self.column, "value": self.value,
                "width": self.width, "decimals": self.decimals,
                "baseline_decimals": self.baseline_decimals,
                "arithmetic_corroborated": self.corroborated,
                "width_pileup": self.width_pileup,
                "reason": (f"该列数值宽度上限 {self.width}，此值卡在上限且只有 "
                           f"{self.decimals} 位小数（该列常见 {self.baseline_decimals} 位），"
                           f"疑似被截断")}


@dataclass
class TruncationReport:
    suspects: list[TruncationSuspect] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    verdict: str = OK

    @property
    def suspect_row_indices(self) -> set[int]:
        return {s.row_index for s in self.suspects}

    def to_dict(self) -> dict:
        return {"columns": self.columns, "suspect_rows": len(self.suspect_row_indices),
                "corroborated": sum(1 for s in self.suspects if s.corroborated),
                "verdict": self.verdict,
                "suspects": [s.to_dict() for s in self.suspects[:50]]}


def detect_truncated_numbers(header: list[str], rows: list[list]) -> TruncationReport:
    """逐列自校准的截断检测。header/rows 与 check_column_alignment 同源。"""
    rep = TruncationReport()
    for col in range(len(header)):
        cells = [(i, _clean_num_text(r[col])) for i, r in enumerate(rows) if col < len(r)]
        nums = [(i, t) for i, t in cells if t and _NUM_TEXT.match(t)]
        if len(nums) < INTEGRITY_TRUNCATION_MIN_SAMPLES:
            continue
        cap = max(len(t) for _, t in nums)
        at_cap = [(i, t) for i, t in nums if len(t) == cap]
        below = [t for _, t in nums if len(t) < cap]
        if not below:
            continue                    # 全部同宽 → 无基线可比，不下结论
        # 宽度上限处是否**堆积**：自然分布里最宽的那一档是尾巴、计数向上递减；
        # 存在硬上限时本该更宽的值全被压到上限那一档，尾巴反而比次宽档还厚。
        # 这一条只作为置信度记录，**不作为过滤条件**——拿它当门槛会让检测取决于
        # "恰好有多少个值足够长"，小表里只有三两个长值时就永远发现不了。
        prev_width = max(len(t) for t in below)
        pileup = len(at_cap) >= sum(1 for t in below if len(t) == prev_width)
        counts: dict[int, int] = {}
        for t in below:
            counts[_decimals(t)] = counts.get(_decimals(t), 0) + 1
        baseline = max(counts, key=lambda d: (counts[d], d))
        if baseline == 0:
            continue                    # 整数列：没有值能比 0 位小数更少
        found = [TruncationSuspect(row_index=i, column=header[col], value=t, width=cap,
                                   decimals=_decimals(t), baseline_decimals=baseline,
                                   width_pileup=pileup)
                 for i, t in at_cap if _decimals(t) < baseline]
        if len(found) < INTEGRITY_TRUNCATION_MIN_SUSPECTS:
            continue
        rep.columns.append(header[col])
        rep.suspects.extend(found)
    if rep.suspects:
        rep.verdict = REVIEW            # 值本身可疑，但行仍可入库并交人工核对
    return rep


def corroborate_truncation(rep: TruncationReport, items: list[dict], **kw) -> TruncationReport:
    """用算术残差为截断嫌疑加一道独立佐证。

    被截断的合价，其 数量×单价 − 合价 必然是**正的、且小于一个计价单位**——丢的
    只是小数位。这个特征与"宽度上限"完全独立，两者同时成立基本可以定性。
    佐证只提高置信度，**不改任何值**。
    """
    for s in rep.suspects:
        if not (0 <= s.row_index < len(items)):
            continue
        a = check_row_arithmetic(items[s.row_index], **kw)
        if a.computed is None or a.total_price is None:
            continue
        residual = a.computed - a.total_price
        s.corroborated = 0 < residual < 1
    return rep


# ─── 合并入口 ────────────────────────────────────────────────────────────────

@dataclass
class IntegrityReport:
    alignment: ColumnAlignmentReport | None = None
    duplicates: DuplicateReport | None = None
    arithmetic: ArithmeticReport | None = None
    truncation: TruncationReport | None = None

    @property
    def verdict(self) -> str:
        return _worst(*(r.verdict for r in (self.alignment, self.duplicates,
                                            self.arithmetic, self.truncation) if r))

    def blocking_rows(self) -> dict[int, list[str]]:
        """行序 → 该行触发的标记。调用方据此逐行拦截，不必整份丢弃。"""
        out: dict[int, list[str]] = {}
        if self.alignment:
            for r in self.alignment.extra_rows:
                out.setdefault(r.row_index, []).append(COLUMN_SHIFT_FLAG)
        if self.duplicates:
            for i in sorted(self.duplicates.duplicate_row_indices):
                out.setdefault(i, []).append(DUPLICATE_FLAG)
        if self.arithmetic:
            for i in self.arithmetic.mismatch_indices:
                out.setdefault(i, []).append(ARITHMETIC_FLAG)
        if self.truncation:
            for i in sorted(self.truncation.suspect_row_indices):
                out.setdefault(i, []).append(TRUNCATION_FLAG)
        return out

    def to_dict(self) -> dict:
        return {"verdict": self.verdict,
                "alignment": self.alignment.to_dict() if self.alignment else None,
                "duplicates": self.duplicates.to_dict() if self.duplicates else None,
                "arithmetic": self.arithmetic.to_dict() if self.arithmetic else None,
                "truncation": self.truncation.to_dict() if self.truncation else None}


def check_table_integrity(header: list[str] | None, rows: list[list] | None,
                          items: list[dict] | None, **dup_kwargs) -> IntegrityReport:
    """四道门一起过。

    header/rows 缺省时跳过列错位与截断检查——两者都需要**原始单元格文本**：
    截断是靠"这个数字写出来有几个字符、几位小数"发现的，值一旦被 float() 解析过，
    `1956390.` 与 `1956390.45` 就再也分不开了。
    """
    tabular = header is not None and rows is not None
    trunc = detect_truncated_numbers(header, rows) if tabular else None
    if trunc is not None and items is not None:
        corroborate_truncation(trunc, items)
    return IntegrityReport(
        alignment=check_column_alignment(header, rows) if tabular else None,
        duplicates=find_duplicate_rows(items, **dup_kwargs) if items is not None else None,
        arithmetic=check_arithmetic(items) if items is not None else None,
        truncation=trunc,
    )


def read_table_rows(path, *, encoding: str = "utf-8") -> tuple[list[str], list[list[str]]]:
    """读 CSV 为 (表头, 原始单元格列表)。**不做任何补齐**——补齐会抹掉错位证据。

    `csv.DictReader` 会把多出的单元格塞进 restkey、缺的补 None，两种异常都变得不可见；
    列错位正是靠"每行到底有几个单元格"发现的，所以这里必须走 `csv.reader`。
    """
    import csv
    from pathlib import Path

    with Path(path).open(encoding=encoding, newline="") as fh:
        rows = [r for r in csv.reader(fh) if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def annotate_items(items: list[dict], report: IntegrityReport) -> list[dict]:
    """把逐行标记写进 items 的 `validation_flags`，供入库门识别。

    只**追加**标记：原值、原字段一个不动（CLAUDE.md §4）。items 的顺序必须与
    传给 check_table_integrity 的 rows 一致，否则行序对不上。
    """
    for idx, flags in report.blocking_rows().items():
        if 0 <= idx < len(items):
            existing = list(items[idx].get("validation_flags") or [])
            items[idx]["validation_flags"] = existing + [f for f in flags if f not in existing]
    return items
