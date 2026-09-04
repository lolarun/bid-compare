"""空格子补位：主路径**什么都没返回**的金额格，交给第二个模型再读一遍（docs/design/33）。

## 为什么需要它

实测（design/33 §2.2、2026-08-23 复测）：泰科龙第 10 页有 9 行，名称和规格都读到了，
数量/单价/合价三个全是空。这 9 行的单价里有 5 个在**整份 Paddle 响应全文里出现 0 次**
——值不在引擎输出里，重构抽取代码一分钱也捞不回来。而这 9 行正好等于该文档
`-26.22%` 合价缺口的 **100%**（¥247,682.78，89 行对 89 条逐位比对）。亨通、浦东同理，
无金额行分别解释其缺口的 ~96% 和 ~67%。

## 四个绑定条件，缺一条就不是这个例外

`.claude/rules/recognition.md` 禁止"文档内混合抽取"。这个模块之所以是**明示例外**
而不是违规，靠的是下面四条同时成立（design/33 §5.1，2026-08-22 用户批准）：

1. **只补 `AMOUNT_EMPTY`（读不到），绝不碰任何已有值。** 覆盖已识别值是 CLAUDE.md §4
   明禁的另一回事。"原文明确不报价"（格子里印着 `/`、`无`）也**不补**——那是合法
   事实，不是缺陷，补它等于逼人编一个金额出来。
2. **逐字段标来源** `field_sources[field] = "llm"`，不冒充 `direct_cell`。
3. **必须过算术恒等式**（`draft_integrity.row_identities_hold`），过不了**丢弃不入库**。
   留空是诚实状态，自洽不了的数字不是。
4. **质量分级不因补位上抬。** 补完仍是 REVIEW，仍要人工确认。

## 这道门为什么是全部风险的答案

design/33 §2.3 的实测最要命的一条：**方向错的时候模型不会响亮地失败，它会返回一个
格式完整、看着合理的错值**——270° 那一轮把税额 `19818.41` 填进了 seq 60 的价税合计，
响应里没有任何东西说明这件事。而同一批答案过一遍恒等式：

| 方向 | 过门行数 |
|---|---|
| 90°（正确） | **9 / 9** |
| 270°（错位） | **0 / 9** |

完美分离，不用调参、不用新阈值。所以方向**不需要单独探测**（§4.2）——把几个方向
都试一遍，谁过门就用谁；一个都不过，就什么都不填。

## 默认关闭

`filler=None` 时本模块不做任何事，7 份快照的回放指标逐字节不变。生产接线由调用方
显式注入 filler，测试因此保持离线可复现。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from apps.api.core.utils import parse_num
from apps.api.services.ingestion.draft_integrity import (
    AMOUNT_EMPTY,
    classify_amount_cell,
    row_identities_hold,
)

log = logging.getLogger(__name__)

# 可补的槽位。**不含 qty 之外的非金额字段**——名称/规格读错是"有值但错"，
# 属于覆盖已有值，design/33 §7 明确排除在外。
FILLABLE_SLOTS: tuple[str, ...] = (
    "qty", "unit_price", "total_price",
    "unit_price_excl_tax", "total_price_excl_tax",
    "unit_price_incl_tax", "total_price_incl_tax",
    "tax_rate", "tax_amount",
)

# 补位来源标记。`DraftRow.field_sources` 的词表里本来就有 "llm" 这一档，一直没人用。
FILL_SOURCE = "llm"
# 行级标记，进疑点收件箱用——补过的行必须在界面上看得出来跟直读的不一样。
FILL_FLAG = "gap_filled"

# 尝试的方向顺序。0° 放最前是因为多数文档本来就是正的，一次就能过门；
# 90°/270° 是实测出现过的两种躺倒方向（design/33 §2.3）。
# **惰性扇出**（§6 决策 1）：按顺序试，谁先过门就停，不是每次都发三次。
DEFAULT_ANGLES: tuple[int, ...] = (0, 90, 270)

PROMPT = """This page is one table from a construction bid quotation. Read the
numeric columns only.

Return CSV with exactly this header and one line per table row, in the order the
rows appear on the page:

seq,qty,unit_price_excl_tax,total_price_excl_tax,tax_rate,tax_amount,unit_price_incl_tax,total_price_incl_tax

Rules:
- `seq` is the row number printed in the leftmost column. It identifies the row;
  never renumber.
- Leave a field EMPTY when you cannot read it. Never guess, never carry a value
  down from the row above, never compute one field from another.
- Write tax rate as a decimal (13% -> 0.13).
- Do not output currency symbols, thousands separators, or any other column.
"""


def get_production_filler(provider) -> Callable[[bytes], str] | None:
    """生产用的 filler：一张页面 PNG → CSV 文本，走既有的 `vl_extract_csv`。

    `provider` 不具备 `vl_extract_csv` 时返回 None——调用方据此把补位整体关掉，
    **不做能力探测后的静默降级**（`.claude/rules/recognition.md`）：补不了就是
    补不了，格子保持空白，不找一个次优路径偷偷顶上。

    这是 design/33 §5.2 说的第二条承重分支：qwen 从"待退役"变成"结构性依赖"。
    删除 qwen 因此不再是清理工作，而是换平台决策。
    """
    # design/41：视觉供应商由 `VISION_CLIENT_VENDOR` 决定。补位是**视觉**调用，
    # 走视觉开关而不是文本开关。切到 mimo 时用它自己的模型，不再读 dashscope
    # 的模型名——那个名字对另一家没有意义。
    from apps.api.core.domain_config import VISION_CLIENT_VENDOR

    if VISION_CLIENT_VENDOR == "mimo":
        from apps.api.intelligence.providers.mimo_vision import get_mimo_vision_provider

        mimo = get_mimo_vision_provider()
        if mimo is not None:
            def _mimo_fill(png: bytes) -> str:
                return mimo.vl_extract_csv([png], PROMPT)
            return _mimo_fill
        log.warning("VISION_CLIENT_VENDOR=mimo 但没有 MIMO_API_KEY，补位回落 dashscope")

    if not hasattr(provider, "vl_extract_csv"):
        return None
    from apps.api.core.config import get_settings

    model = get_settings().DASHSCOPE_QUOTE_VL_MODEL

    def _call(png: bytes) -> str:
        return provider.vl_extract_csv([png], PROMPT, model=model)

    return _call


@dataclass
class FillOutcome:
    """一页的补位结果。**每一项都要能回答"为什么"**，不留只写着成功的黑箱。"""
    page: int
    angle_used: int | None = None          # None = 没有任何方向过门
    rows_targeted: int = 0
    rows_filled: int = 0
    fields_filled: int = 0
    rejected_by_gate: int = 0
    angles_tried: list[int] = field(default_factory=list)
    error: str = ""


@dataclass
class GapFillReport:
    outcomes: list[FillOutcome] = field(default_factory=list)

    @property
    def fields_filled(self) -> int:
        return sum(o.fields_filled for o in self.outcomes)

    @property
    def rows_filled(self) -> int:
        return sum(o.rows_filled for o in self.outcomes)

    def to_dict(self) -> dict:
        return {
            "pages": len(self.outcomes),
            "rows_filled": self.rows_filled,
            "fields_filled": self.fields_filled,
            "detail": [
                {"page": o.page, "angle": o.angle_used, "targeted": o.rows_targeted,
                 "filled": o.rows_filled, "fields": o.fields_filled,
                 "rejected": o.rejected_by_gate, "tried": o.angles_tried,
                 "error": o.error}
                for o in self.outcomes
            ],
        }


# ── ① 找洞 ──────────────────────────────────────────────────────────────────

def find_gaps(rows: Sequence) -> dict[int, list[int]]:
    """`{页码: [该页需要补的 row 在 rows 里的下标]}`。

    判据是 design/33 §4.1 唯一允许的那一条：**这张表有这个列，而引擎在这一格
    什么都没返回**。不是"置信度低"，不是"合价对不上"，不是直觉。

    "这张表有这个列"用同页其他行来判：同一页至少有一行在该槽位取到了值，
    才说明这列存在。否则整列都空只是这份表压根没有这一列（比如无税版式），
    那不是洞。
    """
    by_page: dict[int, list[int]] = {}
    for i, r in enumerate(rows):
        page = getattr(getattr(r, "source_ref", None), "page", 0) or 0
        if page:
            by_page.setdefault(page, []).append(i)

    out: dict[int, list[int]] = {}
    for page, idxs in by_page.items():
        present = {
            s for s in FILLABLE_SLOTS
            if any(str(rows[i].fields.get(s) or "").strip() for i in idxs)
        }
        if not present:
            continue
        targets = [i for i in idxs if _missing_slots(rows[i], present)]
        if targets:
            out[page] = targets
    return out


def _missing_slots(row, present: set[str]) -> list[str]:
    """这一行里"该有值却读不到"的槽位。

    `classify_amount_cell` 区分三态，这里**只认 `AMOUNT_EMPTY`**：格子里印着
    `/`、`无` 的是 `AMOUNT_NOT_QUOTED`，那是投标方明确不报此项，合法事实，
    补它就是逼人编数字（CLAUDE.md 明令两者不得合并成同一个空值语义）。
    """
    out = []
    for s in present:
        v = row.fields.get(s)
        if str(v or "").strip():
            continue
        if classify_amount_cell(v) == AMOUNT_EMPTY:
            out.append(s)
    return out


# ── ② 读回来 ────────────────────────────────────────────────────────────────

def _parse_fill_csv(text: str) -> dict[str, dict[str, str]]:
    """模型返回的 CSV → `{seq: {slot: value}}`。解析失败返回空 dict，不抛。"""
    import csv as _csv

    out: dict[str, dict[str, str]] = {}
    try:
        reader = _csv.DictReader(io.StringIO((text or "").strip()))
        for rec in reader:
            seq = str(rec.get("seq") or "").strip()
            if not seq:
                continue
            vals = {k: str(v).strip() for k, v in rec.items()
                    if k in FILLABLE_SLOTS and str(v or "").strip()}
            if vals:
                out[seq] = vals
    except Exception:                                              # noqa: BLE001
        log.warning("补位返回内容解析失败，本页放弃", exc_info=True)
        return {}
    return out


def _rotate(png: bytes, angle: int) -> bytes:
    if angle % 360 == 0:
        return png
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        buf = io.BytesIO()
        img.rotate(-angle, expand=True).convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


# ── ③ 过门并写回 ────────────────────────────────────────────────────────────

def _apply_to_row(row, vals: dict[str, str], *, tolerance: float) -> int:
    """把一行的候选值过门后写回。返回真正写进去的字段数；0 = 整行被拒。

    **先在副本上试算，过门了才落到真行上。** 直接写再回滚会在中途留下半填状态，
    而这一行此刻正在被别的判据读——诚实的空比一闪而过的错值更重要。
    """
    candidate = dict(row.fields)
    touched: list[str] = []
    for slot, v in vals.items():
        if str(candidate.get(slot) or "").strip():
            continue            # 条件①：绝不覆盖已有值
        if classify_amount_cell(candidate.get(slot)) != AMOUNT_EMPTY:
            continue            # "明确不报价"不是洞
        if parse_num(v) is None:
            continue            # 模型返回了非数字，丢弃
        candidate[slot] = v
        touched.append(slot)
    if not touched:
        return 0
    # 条件③：整行必须自圆其说，否则**整行丢弃**——不是逐字段挑能过的留下。
    # 逐字段挑会让一行由"直读的一半 + 挑剩的一半"拼成，那个组合谁也没验证过。
    if not row_identities_hold(candidate, tolerance=tolerance):
        return 0
    for slot in touched:
        row.fields[slot] = candidate[slot]
        row.field_sources[slot] = FILL_SOURCE       # 条件②
    if FILL_FLAG not in row.validation_flags:
        row.validation_flags.append(FILL_FLAG)
    return len(touched)


def fill_gaps(
    rows: Sequence,
    *,
    filler: Callable[[bytes], str] | None,
    render_page: Callable[[int], bytes | None],
    angles: Sequence[int] = DEFAULT_ANGLES,
    tolerance: float | None = None,
) -> GapFillReport:
    """对 `rows` 就地补位，返回报告。`filler=None` 时什么都不做。

    `filler`: 一张页面 PNG → CSV 文本（生产侧是 `vl_extract_csv` 的单页封装）。
    `render_page`: 页码（1 起）→ PNG 字节，拿不到返回 None。

    **质量分级不在这里动**（条件④）。本函数只改 `fields`/`field_sources`/
    `validation_flags`；`compute_quality` 在调用方那边照常跑，补过的行仍然带着
    `gap_filled` 标记，该 REVIEW 还是 REVIEW。
    """
    report = GapFillReport()
    if filler is None:
        return report
    if tolerance is None:
        from apps.api.core.domain_config import EXTRACTION_ARITHMETIC_TOLERANCE
        tolerance = EXTRACTION_ARITHMETIC_TOLERANCE

    gaps = find_gaps(rows)
    for page in sorted(gaps):
        idxs = gaps[page]
        outcome = FillOutcome(page=page, rows_targeted=len(idxs))
        try:
            png = render_page(page)
        except Exception as exc:                                   # noqa: BLE001
            outcome.error = f"渲染失败: {exc}"
            report.outcomes.append(outcome)
            continue
        if not png:
            outcome.error = "渲染不出这一页"
            report.outcomes.append(outcome)
            continue

        log.info("补位第 %d 页开始，待补 %d 行，方向顺序 %s", page, len(idxs), list(angles))
        for angle in angles:
            outcome.angles_tried.append(angle)
            # **旋转失败和调用失败要分开归因。** 前者是我们自己的 bug（图片坏了、
            # PIL 出问题），后者是预期内的正常结果（0° 被安全审查拒绝，多半是
            # 红章，见 design/33 §2.3）。混在一个 except 里，前者会伪装成后者，
            # 日志上看永远是"模型不给力"，真正的缺陷一直查不出来。
            try:
                image = _rotate(png, angle)
            except Exception as exc:                               # noqa: BLE001
                log.warning("补位第 %d 页旋转 %d° 失败（这是本地缺陷，不是模型问题）：%s",
                            page, angle, exc)
                continue
            # 2026-08-27：这里之前完全没有"开始调用"这条日志——一次调用卡住时，
            # 日志上跟"这一页还没轮到"长得一模一样。有了这行，进程还活着但某次
            # 调用挂起 vs. 整个任务已经死了，从日志时间戳上能分清。
            log.info("补位第 %d 页 %d° 调用模型…", page, angle)
            try:
                answer = _parse_fill_csv(filler(image))
            except Exception as exc:                               # noqa: BLE001
                log.info("补位第 %d 页 %d° 模型调用未返回：%s", page, angle, exc)
                continue
            if not answer:
                continue

            filled_rows = fields = rejected = 0
            for i in idxs:
                seq = str(rows[i].fields.get("seq") or "").strip()
                vals = answer.get(seq)
                if not vals:
                    continue
                n = _apply_to_row(rows[i], vals, tolerance=tolerance)
                if n:
                    filled_rows += 1
                    fields += n
                else:
                    rejected += 1
            if filled_rows:
                outcome.angle_used = angle
                outcome.rows_filled = filled_rows
                outcome.fields_filled = fields
                outcome.rejected_by_gate = rejected
                break       # 惰性扇出：过门了就不再试别的方向
            outcome.rejected_by_gate = rejected

        if outcome.angle_used is None and not outcome.error:
            # 一个方向都没过门 —— 什么都不填，如实记下来。
            outcome.error = "没有任何方向通过算术校验，本页保持留空"
        report.outcomes.append(outcome)

    return report
