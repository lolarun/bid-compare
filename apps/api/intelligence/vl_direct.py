"""vl_direct.py — 报价清单识别器：整份页面图像 → 视觉模型 → CSV → ExtractionDraft。

**报价识别的唯一路径**（2026-08-10 起）。legacy 的报价分支（OCR → HTML →
TableGrid → LLM）已归档，`QUOTE_RECOGNIZER` 开关随之移除——留着它就等于留着
"这次结果是哪条路出来的"这个疑问，而实测 provider 缺一个方法就会静默换路。
`recognize_tables` 仍在，但只服务招标清单（services/tender_pdf.py），
那一侧尚无 VL 实现，见 docs/design/21 Phase 2。

## 为什么整份一次调用

续页表头在前页、重复副本要跨页才看得见、序号连续性也只有整份才能校验。
逐页送等于把这条路最大的优势丢掉。

## 提示词的四条规则

前三条是"输出什么"类的业务语义（模型从图像无从推断），实测有效；
第四条页码是生产新增的——脚本版没有它，导致每行无法归属到页，
`document_row_index` / 顺序直连 / 定向重读全都拿不到输入。

**曾有过第五条 uncertainty 列，已删（2026-08-10）。** 加它的理由是"模型破坏
CSV 格式是因为没地方放疑问"，六次单变量 A/B 证伪：无此列的对照臂同样没有格式
崩溃，两臂缺格行 0/0/0、金额差 0/−52/0。真因是**方向**（那次只转 3 页而应转
13 页），"出声思考"是症状不是病因。它的输出量也不像信号——两轮里在 0–70 行
之间乱跳，一次 273 行标了 70 行。不要凭"零成本"把它加回来：加对照臂之前
不要做这类归因。

**"告诉模型怎么看"的约束一律不要**："按原始左右顺序输出列"曾让侧向页整张表转置。

## 方向

侧躺（90°/270°）的页读不出来，必须在送进去之前转正；180° 倒置单页无害，但同一份内
方向混杂时实测损失 −129,532（0.63%）。判定不稳定是已知事实——无过半共识的页
**不转并标 REVIEW**，绝不猜一个角度。

## 可测试性

模型调用通过 `vl_call` / `orient_call` 注入，本模块不直接依赖 provider 实现。
这样单测无需网络，也让输入输出可被快照重放（`.claude/rules/recognition.md`）。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from apps.api.core.config import get_settings
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.extraction_draft import (
    DraftRow,
    ExtractionDraft,
    PageMetric,
    SourceRef,
    build_row_ledger,
    compute_quality,
)
from apps.api.services.draft_integrity import (
    AMOUNT_NOT_QUOTED,
    check_column_alignment,
    check_sequence_continuity,
    classify_amount_cell,
    detect_truncated_numbers,
)

log = logging.getLogger(__name__)

# 每批送多少页去渲染。渲染必须懒加载、分批释放（.claude/rules/recognition.md）：
# 一次性把 53 页全渲成全分辨率 PNG 会把内存峰值顶穿。
RENDER_BATCH = 8

# 方向预检送的图必须**大幅缩小**。它判的是"这页文字是不是横躺着"——一个粗判断，
# 不需要认字。而它的载荷是抽取的 12 倍（页数 × 4 个旋转版本 × 3 轮投票），
# 用全分辨率送会让方向预检占掉整条链路的绝大部分时间：实测 19 页的文档
# 全分辨率要发 638MB，抽取本身才 53MB。
#
# 400px 这个数来自离线基线：验证出「明细求和差 +0.04 元」的那次用的是
# scale=0.30，对应长边约 253–357px。这里取 400 略宽松一点。
# 改动此值等于改变已验证配置，必须重新跑准确率对照。
ORIENT_PROBE_MAX_EDGE_PX = 400

PROMPT_QUOTE_CSV = """请将这份投标文件中的报价清单导出为 CSV 格式给我。只返回 CSV，不要其他说明。

另外遵守四条规则：
1. 小计/合计/总计行要保留，不要跳过。第一列固定为 row_type，标注每行类型：
   明细行填 detail，小计行填 subtotal，总计/合计行填 total。
2. 只转录文档上确实写着的数字。任何单元格为空或看不清就留空，
   不要用数量×单价补算合价，也不要补算任何其他数字。
   原文明确写"不报价"的（如 / 、无、N/A），照原样填这个符号，不要留空。
3. 如果同一份清单在文件里重复出现（例如正本与副本、汇总与明细），照实全部输出，
   不要合并也不要丢弃。倒数第二列固定为 copy_no，标注该行属于第几份（1、2……）。
4. 最后一列固定为 page，填该行来自第几页（按我给你的图像顺序，从 1 开始）。"""

PROMPT_ORIENT = """下面每一页给了 4 个旋转版本，标签形如 PAGE_<页号>_ROT_<角度>。
对每一页，挑出文字正立、可以正常阅读的那个角度。
只返回 CSV 两列：page,rotation。"""

# ─── 报价封面元信息（声明总价 / 投标单位 / 税率）─────────────────────────────
#
# 与采购清单是两件事：清单逐行、封面是文档级标量。这几个字段喂给两处生产判据：
# 声明总价核对门（quote_confirmation_service._build_checksum）和供应商识别。
# 此前 VL 路径从不产出——门本身在 §A3 已验证逻辑正确，只是从未接到过输入，
# 生产上对任何 PDF 报价都是 unknown/不阻断，见 docs/design/21 §2.2/§2.3。
PROMPT_QUOTE_META = """这是一份投标文件的首页或汇总页。请告诉我下面四项，每行一个，格式 key: value：

supplier_name      投标单位/报价单位全称
bid_total          投标总价（只要数字，不要货币符号和单位）
bid_total_basis    该总价是否含税：tax_included / tax_excluded / unknown
tax_rate           税率，小数形式（如 13% 写 0.13）

文档上没写的就留空，不要推测。只返回这四行，不要其他说明。"""

QUOTE_META_PAGES = 2   # 首页够了，为四个标量渲染整份是纯浪费


def parse_quote_meta(text: str) -> dict:
    """`key: value` 逐行 → 字典。与 vl_tender.parse_tender_meta 同一形态，
    分开实现是因为字段集合不同（供应商/总价 vs 项目名称/日期）。

    数值字段（bid_total/tax_rate）缺省是 **None 不是空串**：下游
    `declared_total_diff`（extraction_draft.py）拿它跟明细之和做减法，
    传空串会直接 TypeError——这不是防御性编程，是类型契约。
    """
    out: dict = {"supplier_name": "", "bid_total": None,
                "bid_total_basis": "unknown", "tax_rate": None}
    for line in (text or "").splitlines():
        if ":" not in line and "：" not in line:
            continue
        key, _, value = line.replace("：", ":").partition(":")
        key = key.strip().lower()
        if key not in out:
            continue
        value = value.strip()
        if key == "bid_total":
            out[key] = _num(value)
        elif key == "tax_rate":
            out[key] = _num(value)
        else:
            out[key] = value
    if out["bid_total_basis"] not in ("tax_included", "tax_excluded", "unknown"):
        out["bid_total_basis"] = "unknown"
    return out


def extract_quote_meta(images: list[bytes], vl_call: VLCall) -> dict:
    """封面 1-2 页 → 声明总价等四项。失败留空——清单才是主线，
    不该让整份识别因为封面读不出而失败。"""
    empty = parse_quote_meta("")   # 单一来源的"空"形状，不在两处各写一份
    if not images:
        return empty
    try:
        return parse_quote_meta(vl_call(images[:QUOTE_META_PAGES], PROMPT_QUOTE_META))
    except Exception:                                            # noqa: BLE001
        log.warning("报价封面元信息抽取失败，四项留空", exc_info=True)
        return empty

# 列名映射。**不假定固定列名**——提示词有意让模型用文档自己的表头（这正是泛化要的），
# 消费方必须自己映射。中英都认：同一个模型在不同文档上会自发切换表头语言。
#
# 「这是不含税列」/「这是含税列」的判据。实测模型写法不受控：excl / ex_tax /
# exclusive / 税前 都出现过，漏一种就意味着不含税的值被装进含税槽位，偏差恰好
# 等于税率。
_EXCL = ("不含税", "未含税", "税前",
         "excl", "ex_tax", "ex-tax", "extax", "pre_tax", "pretax", "net_of_tax")
_INCL = ("含税", "价税合计", "incl", "inc_tax", "in-tax", "intax", "with_tax")

_SLOTS: dict[str, list[tuple[str, ...]]] = {
    "name":        [("名称",), ("品名",), ("材料",), ("name",)],
    "spec":        [("规格",), ("型号",), ("spec",), ("model",)],
    "unit":        [("单位",), ("unit",)],
    "qty":         [("数量",), ("工程量",), ("quantity",), ("qty",)],
    "brand":       [("品牌",), ("厂家",), ("brand",)],
    # 中英模式必须**对称**：模型在不同文档上会自发切换表头语言（实测），
    # 只在一种语言里认得出税基，等于在另一种语言下静默丢失税基。
    #
    # 含税/不含税槽位在字典里排在通用槽位**之前**是有意的：`map_columns` 逐槽位
    # 认领列，先到先得。若通用 unit_price/total_price 先跑，"含税单价" 会因为
    # 含"单价"子串被通用槽位吞掉——那正是曾经发生的 bug（见 domain_config 同名
    # 常量旁的注释）：通用槽位拿到含税值，derive_price_basis 却因为看不到
    # unit_price_incl_tax 而判成 excl_tax，比价系统性偏低整段税率。
    "unit_price_incl_tax":  [("含税单价",), ("单价", "含税"),
                             ("unit_price", "incl"), ("unit_price", "inc_tax"),
                             ("unit_price", "with_tax"), ("price", "inc_tax")],
    "total_price_incl_tax": [("价税合计",), ("含税合价",), ("含税金额",),
                             ("total", "incl"), ("total", "inc_tax"),
                             ("total", "with_tax"), ("amount", "inc_tax")],
    "unit_price_excl_tax":  [("不含税单价",), ("单价", "不含税"),
                             ("unit_price", "excl"), ("unit_price", "ex_tax"),
                             ("unit_price", "pre_tax"), ("price", "excl"),
                             ("price", "ex_tax"), ("price", "net")],
    "total_price_excl_tax": [("不含税合价",), ("合计", "不含税"), ("合价", "不含税"),
                             ("total", "excl"), ("total", "ex_tax"),
                             ("total", "pre_tax"), ("amount", "excl"),
                             ("amount", "ex_tax"), ("subtotal", "net")],
    # 通用槽位：**只吸收既非含税也非不含税标注的列**（如"单价""合价"这种单一价格列）。
    # 与 legacy 提示词的"若表头已区分，则留null"是同一条契约——见下面 _TAX_SENSITIVE
    # 的 _INCL/_EXCL 双向排斥。
    "unit_price":  [("综合单价",), ("单价",),
                    ("unit_price",), ("unit", "price"), ("price",)],
    "total_price": [("合价",), ("金额",), ("总价",),
                    ("total_price",), ("total", "amount"), ("amount",)],
    "tax_rate":    [("税率",), ("tax_rate",)],
    "tax_amount":  [("税额",), ("tax_amount",)],
    "remark":      [("备注",), ("remark",), ("note",)],
    "seq":         [("序号",), ("seq",)],
    "row_type":    [("row_type",)],
    "copy_no":     [("copy_no",)],
    "page":        [("page",), ("页码",), ("页",)],
}
# 通用槽位对 _EXCL 与 _INCL 都要排斥——单向排斥（只挡 excl）正是 A2 缺陷的成因：
# "含税单价" 不含 _EXCL 关键词，会被通用槽位放行、抢在 unit_price_incl_tax 之前
# 认领掉该列（"含税单价"含"单价"子串，能匹配通用槽位的宽松 tier）。
_TAX_SENSITIVE = {"unit_price", "total_price"}

# 子串匹配还带来另一个坑：**"不含税单价" 本身包含 "含税单价" 这个子串**（不 + 含税单价），
# 所以 unit_price_incl_tax 的 tier ("含税单价",) 会误吃"不含税"列。必须让 incl_tax
# 槽位反过来排斥 _EXCL 标记——不对称是有意的：
#   - unit_price/total_price（通用槽位）要挡两个方向：既不能吞"含税单价"（那是
#     A2 缺陷本体），也不能吞"不含税单价"（那是修复前就有的旧防线）。
#   - unit_price_incl_tax 只需挡 _EXCL：它自己的 tier 就是"含税"字样，不会误配到
#     别的不相关列。
#   - unit_price_excl_tax **不能**挡 _INCL：_INCL 含"含税"，而"不含税"本身就
#     包含"含税"子串——挡了就等于挡了它自己要匹配的列，实测复现过这个反向坑。
_EXCL_GUARDED = {"unit_price", "total_price",
                 "unit_price_incl_tax", "total_price_incl_tax"}
_INCL_GUARDED = {"unit_price", "total_price"}

# 「税额」列绝不能落进任何价格槽位。它危险的地方在于**下游察觉不到**：
# 税额 ≈ 不含税合价 × 税率，本身自洽，逐行算术校验照样通过，只是整份金额偏小。
# 触发路径实测存在：末档模式 ("amount",) 会命中 tax_amount。
_TAX_AMOUNT_MARKERS = ("税额", "tax_amount", "taxamount", "tax amt", "vat_amount")
_PRICE_SLOTS = {"unit_price", "total_price",
                "unit_price_excl_tax", "total_price_excl_tax",
                "unit_price_incl_tax", "total_price_incl_tax"}


def map_columns(headers: Sequence[str], *,
                slots: dict[str, list[tuple[str, ...]]] | None = None) -> dict[str, str]:
    """表头 → 槽位。含税/不含税同时存在时必须选对那一列，否则把税前税后混为一谈。"""
    lower = [(h or "").lower() for h in headers]
    out: dict[str, str] = {}
    # **一列只能认领一次。** 「规格型号」同时含「规格」和「型号」，无互斥时会同时
    # 落进 spec 和 model 两个槽位，下游看到两个字段值相同却不知道它们是同一列。
    # 七份真实报价表头实测：加互斥后映射零变化——它修的是歧义，不改变已有行为。
    taken: set[str] = set()
    for slot, tiers in (slots or _SLOTS).items():
        for tier in tiers:
            for h, lo in zip(headers, lower):
                if not h or h in taken:
                    continue
                if slot in _PRICE_SLOTS and any(x in lo for x in _TAX_AMOUNT_MARKERS):
                    continue
                if slot in _EXCL_GUARDED and any(x in lo for x in _EXCL):
                    continue
                if slot in _INCL_GUARDED and any(x in lo for x in _INCL):
                    continue
                if all(k in lo for k in tier):
                    out[slot] = h
                    taken.add(h)
                    break
            if slot in out:
                break
    return out


# ─── 调用契约（注入点）────────────────────────────────────────────────────────
# 返回模型原始文本；失败请抛异常，不要吞掉——静默失败会变成"这份文档只有这些行"。
VLCall = Callable[[list[bytes], str], str]
OrientCall = Callable[[list[tuple[str, bytes]], str], str]

# (取槽位值, 该行原始单元格, 表头→槽位映射) → 字段字典。
# 报价与招标的差异全部收敛在这个函数里，解析与结构门两边共用。
FieldBuilder = Callable[[Callable[[str], str], dict, dict], dict]


def build_quote_fields(cell, _raw_cells: dict, _cmap: dict) -> dict:
    """报价行字段。"""
    fields = {
        "seq": cell("seq").strip(),
        "name": cell("name").strip(),
        "spec": cell("spec").strip(),
        "unit": cell("unit").strip(),
        "brand": cell("brand").strip(),
        "remark": cell("remark").strip(),
        "qty": _num(cell("qty")),
        "unit_price": _num(cell("unit_price")),
        "total_price": _num(cell("total_price")),
        "unit_price_excl_tax": _num(cell("unit_price_excl_tax")),
        "total_price_excl_tax": _num(cell("total_price_excl_tax")),
        # A2 修复：VL 此前从不产出这两个字段（槽位表只有通用/不含税，没有含税），
        # 导致双列（含税+不含税）文档在 derive_price_basis 里判成 excl_tax，
        # 评标价系统性取了不含税值——下游（pipeline.py:229/232）早就在读这两个
        # 字段，只是识别层从没填过。
        "unit_price_incl_tax": _num(cell("unit_price_incl_tax")),
        "total_price_incl_tax": _num(cell("total_price_incl_tax")),
        "tax_rate": _num(cell("tax_rate")),
        "tax_amount": _num(cell("tax_amount")),
    }
    # 「原文明确不报价」必须在原始文本还在的时候判定：_num 把「/」和空白
    # 一律变成 None，两种语义就此不可分辨（前者合法，后者是缺陷）。
    fields["not_quoted"] = any(
        classify_amount_cell(cell(s)) == AMOUNT_NOT_QUOTED
        for s in ("total_price", "total_price_excl_tax", "total_price_incl_tax")
    )
    return fields


@dataclass
class _ParsedRow:
    row_type: str
    page: int | None
    copy_no: str
    raw_cells: dict
    fields: dict
    extra: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)


def _norm_row_type(raw: str) -> str:
    """CSV 的 row_type → DraftRow 词表。**认不出来的一律当明细**，不能丢——
    静默丢弃未知标签会把召回凭空做高。"""
    v = (raw or "").strip().lower()
    if v in ("subtotal", "小计"):
        return "subtotal"
    if v in ("total", "grand_total", "合计", "总计", "总价"):
        return "grand_total"
    return "quote_line"


def _num(x):
    if x is None or x == "":
        return None
    try:
        return float(re.sub(r"[,，¥￥$\s　]", "", str(x)))
    except (TypeError, ValueError):
        return None


def parse_csv(text: str, page_count: int, *,
              slots: dict[str, list[tuple[str, ...]]] | None = None,
              field_builder: "FieldBuilder | None" = None,
              ) -> tuple[list[_ParsedRow], list[str], dict]:
    """CSV 文本 → 结构化行 + 表头 + 诊断。

    走 `csv.reader` 而不是 `DictReader`：后者把多出的格塞进 restkey、缺的补 None，
    **列错位的证据就此消失**，而那是入库门唯一的判据来源。

    `slots` / `field_builder` 让招标清单复用同一套解析与结构门：两种文档的**行为
    差异只在"有哪些列、每列叫什么"**，列错位、截断、序号连续性、页码归属这些判据
    完全一样。复制一份解析器出来只会让两边慢慢漂移。
    """
    rows = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        return [], [], {"reason": "empty_or_header_only", "line_count": len(rows)}
    header = [h.strip() for h in rows[0]]
    body = rows[1:]
    cmap = map_columns(header, slots=slots)
    align = check_column_alignment(header, body)
    trunc = detect_truncated_numbers(header, body)
    trunc_rows = trunc.suspect_row_indices

    def cell(row: list[str], slot: str) -> str:
        col = cmap.get(slot)
        if not col:
            return ""
        i = header.index(col)
        return (row[i] if i < len(row) else "") or ""

    out: list[_ParsedRow] = []
    shifted = align.bad_row_indices
    for i, row in enumerate(body):
        page_raw = re.sub(r"\D", "", cell(row, "page"))
        page = int(page_raw) if page_raw.isdigit() and 1 <= int(page_raw) <= page_count else None
        raw_cells = {h: (row[j] if j < len(row) else "") for j, h in enumerate(header) if h}
        used = set(cmap.values())
        builder = field_builder or build_quote_fields
        fields = builder(lambda slot: cell(row, slot), raw_cells, cmap)
        fields["parser_mode"] = "vl_direct"
        flags: list[str] = []
        if i in shifted:
            flags.append("column_shift")
        if i in trunc_rows:
            flags.append("value_truncated")
        out.append(_ParsedRow(
            row_type=_norm_row_type(cell(row, "row_type")),
            page=page,
            copy_no=cell(row, "copy_no").strip(),
            raw_cells=raw_cells,
            fields=fields,
            extra={h: v for h, v in raw_cells.items() if h not in used},
            flags=flags,
        ))
    # 「有没有把钱读到」是独立于模式列表的判据：任一合价槽位映射上即算有。
    # 单价不算——只有单价没有合价时，合价必须由 数量×单价 推出，而那正是
    # 禁止的静默派生（quote_fact.py）。
    has_price = any(cmap.get(s) for s in ("total_price", "total_price_excl_tax"))
    # 没进任何槽位、但整列看着像金额的表头——映射失败时它们就是丢掉的那笔钱。
    # 阈值取"半数以上单元格能解析成数"，避免把序号/数量列也算进来是次要的：
    # 这个字段只用于给人看的诊断，不参与判定。
    unmapped_numeric: list[str] = []
    mapped = set(cmap.values())
    for j, h in enumerate(header):
        if not h or h in mapped:
            continue
        vals = [row[j] for row in body if j < len(row) and (row[j] or "").strip()]
        if vals and sum(1 for v in vals if _num(v) is not None) > len(vals) / 2:
            unmapped_numeric.append(h)

    diag = {"header": header, "column_map": cmap,
            "alignment": align.to_dict(), "truncation": trunc.to_dict(),
            "rows_without_page": sum(1 for r in out if r.page is None),
            "has_price_column": has_price,
            "unmapped_numeric_columns": unmapped_numeric}
    return out, header, diag


def _assign_pages(parsed: list[_ParsedRow], page_count: int) -> None:
    """页码缺失时按**已知页码的前后文**补，并如实标记补过。

    绝不猜到一个具体页就当成事实：补出来的页码只用于排序与定向重读的粗定位，
    行级证据仍以模型给出的为准。补不出来的保持 None——**None 是诚实的**。
    """
    last = None
    for r in parsed:
        if r.page is not None:
            last = r.page
        elif last is not None:
            r.page = last
            r.flags.append("page_inferred")


def build_draft(text: str, *, file_path: str, page_count: int,
                processed_pages: list[int], supplier_name: str = "",
                rotations: dict[int, int] | None = None,
                unresolved_pages: Sequence[int] = (),
                declared_total: float | None = None,
                doc_type: str = "quote",
                slots: dict[str, list[tuple[str, ...]]] | None = None,
                field_builder: FieldBuilder | None = None) -> ExtractionDraft:
    """模型返回的 CSV → ExtractionDraft（含逐行来源、质量与行数台账）。

    `doc_type` 只影响**价格相关的门**：招标采购清单本来就没有价格列（那是留给
    投标人填的空表），对它做"读到行却读不到钱"的判定会把每一份都误判成 BLOCKED。
    结构、截断、序号、页码归属这些判据两种文档完全一样，不区分。
    """
    parsed, header, diag = parse_csv(text, page_count,
                                     slots=slots, field_builder=field_builder)
    _assign_pages(parsed, page_count)

    rows: list[DraftRow] = []
    per_page_rows: dict[int, int] = {}
    for i, p in enumerate(parsed):
        page = p.page or 0
        per_page_rows[page] = per_page_rows.get(page, 0) + 1
        rows.append(DraftRow(
            row_index=i,
            row_type=p.row_type,
            raw_cells=p.raw_cells,
            fields=dict(p.fields, copy_no=p.copy_no,
                        document_row_index=i + 1,
                        page_row_index=per_page_rows[page]),
            # bbox 不可得：整份一次调用拿不到像素坐标。按规则不得宣称行级像素追溯，
            # 故 bbox 留 None，只给页/行序。
            source_ref=SourceRef(page=page, table=0, row=per_page_rows[page]),
            validation_flags=list(p.flags),
            field_sources={k: ("direct_cell" if v not in (None, "") else "missing")
                           for k, v in p.fields.items()},
            extra_fields=p.extra,
        ))

    metrics = [
        PageMetric(page=p, page_index=p - 1, role="quote_table",
                   input_mode="vl_direct", table_count=1,
                   row_count=per_page_rows.get(p, 0),
                   expected_rows=per_page_rows.get(p, 0),
                   extracted_rows=per_page_rows.get(p, 0),
                   rotation_applied=(rotations or {}).get(p, 0))
        for p in processed_pages
    ]
    quality = compute_quality(
        rows, metrics, total_pages=page_count, target_pages=list(processed_pages),
        declared_total=declared_total, rendered_pages=len(processed_pages),
        ocr_success_pages=len(processed_pages),
    )
    # 方向没定下来的页必须进 REVIEW：不转就可能整页读不出，而我们并不知道读没读出来。
    # QualityReport 用 blocking_reasons 同时承载 review 提示与阻断原因，状态词表是
    # PASS / REVIEW / BLOCKED。已经 BLOCKED 的不要降级成 REVIEW。
    if unresolved_pages:
        quality.blocking_reasons = list(quality.blocking_reasons or []) + [
            f"orientation_unresolved_pages={list(unresolved_pages)[:10]}"]
        if quality.status == "PASS":
            quality.status = "REVIEW"

    # 价格列没映射上 → BLOCKED。这是**不依赖模式列表**的兜底，必须有：
    # 表头文字由模型决定、写法不受控（实测 excl / ex_tax / 不含税 / 税前 都出现过），
    # _SLOTS 的模式列表永远补不完。实测一次真实失败——模型把表头译成
    # unit_price_ex_tax / total_inc_tax，total_price 一个都没匹配上，
    # 89 行合价全空，金额短 824,915 元（88.5%），而结构门判 ok、逐行算术无异常。
    # 「读到了行却读不到钱」符合 CLAUDE.md §4 BLOCKED 的「无有效报价」。
    # 行数守恒：台账在 VL 路径上是同义反复（expected 与 extracted 同源），
    # 序号是文档自印的、不由抽取质量决定，是目前唯一的独立判据。
    # **覆盖率不足时如实说"没有判据"，不得当成"没有问题"**（docs/design/21 §2.1）。
    seq = check_sequence_continuity(
        [{"seq": r.fields.get("seq")} for r in rows if r.row_type == "quote_line"])
    diag["sequence"] = seq.to_dict()
    if seq.verdict == "blocked":
        quality.blocking_reasons = list(quality.blocking_reasons or []) + [seq.reason]
        quality.status = "BLOCKED"
    elif seq.verdict in ("review", "not_applicable"):
        quality.blocking_reasons = list(quality.blocking_reasons or []) + [
            seq.reason if seq.verdict == "review"
            else f"row_conservation_unverifiable: {seq.reason}"]
        if quality.status == "PASS":
            quality.status = "REVIEW"

    lost = diag.get("unmapped_numeric_columns") or []
    if doc_type == "quote" and rows and not diag.get("has_price_column", True):
        quality.blocking_reasons = list(quality.blocking_reasons or []) + [
            f"no_price_column_mapped; header={header[:12]}; "
            f"unmapped_numeric_columns={lost[:6]}"]
        quality.status = "BLOCKED"

    draft = ExtractionDraft(
        doc_type=doc_type, source_file=file_path,
        page_count=page_count, processed_page_count=len(processed_pages),
        target_pages=list(processed_pages), rows=rows,
        meta={"supplier_name": supplier_name, "recognizer": "vl_direct",
              "doc_type": doc_type,
              "csv_header": header, "diagnostics": diag,
              "rotations": dict(rotations or {}),
              "orientation_unresolved": list(unresolved_pages)},
        quality=quality,
        ledger=build_row_ledger(metrics, list(processed_pages), len(rows)),
    )
    return draft


# ─── 方向预检 ────────────────────────────────────────────────────────────────

def detect_rotations(images: dict[int, bytes], orient_call: OrientCall, *,
                     votes: int = 3) -> tuple[dict[int, int], list[int]]:
    """返回 (页 → 需旋转角度, 未达成共识的页)。

    四选一（把 4 个旋转版本并排让模型挑正立的那张）——实测这种"比较候选"的问法
    比"问需要转多少度"和"这页倒了吗"都稳。但**它依然不稳**：同份同配置三次跑出
    3/10、10/10、10/10。投票只降低崩塌概率，故未过半的页交回调用方标 REVIEW。
    """
    from PIL import Image

    def probe_base(png: bytes) -> bytes:
        """先缩小再生成 4 个旋转版本——缩一次、复用四次，别缩四次。"""
        with Image.open(io.BytesIO(png)) as im:
            im = im.convert("RGB")
            longest = max(im.size)
            if longest > ORIENT_PROBE_MAX_EDGE_PX:
                k = ORIENT_PROBE_MAX_EDGE_PX / longest
                im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG", compress_level=3)
            return buf.getvalue()

    def rotated(png: bytes, deg: int) -> bytes:
        if deg == 0:
            return png
        with Image.open(io.BytesIO(png)) as im:
            buf = io.BytesIO()
            im.convert("RGB").rotate(-deg, expand=True).save(buf, "PNG")
            return buf.getvalue()

    pages = sorted(images)
    # 缩略图只算一次；投票 3 轮复用同一份，否则等于把缩放成本也乘以轮数。
    probes = {p: probe_base(images[p]) for p in pages}
    tally: dict[int, dict[int, int]] = {p: {} for p in pages}
    for _ in range(max(1, votes)):
        parts: list[tuple[str, bytes]] = []
        for p in pages:
            for deg in (0, 90, 180, 270):
                parts.append((f"PAGE_{p}_ROT_{deg}", rotated(probes[p], deg)))
        try:
            text = orient_call(parts, PROMPT_ORIENT)
        except Exception:                                  # noqa: BLE001
            log.exception("orientation probe failed; treating this round as no answer")
            continue
        for line in (text or "").replace("```", "").splitlines():
            cells = [c.strip() for c in line.split(",")]
            if len(cells) < 2:
                continue
            pg, deg = re.sub(r"\D", "", cells[0]), re.sub(r"\D", "", cells[1])
            if pg.isdigit() and deg.isdigit() and int(pg) in tally and int(deg) in (0, 90, 180, 270):
                tally[int(pg)][int(deg)] = tally[int(pg)].get(int(deg), 0) + 1

    out: dict[int, int] = {}
    unresolved: list[int] = []
    for p, counts in tally.items():
        if not counts:
            unresolved.append(p)
            continue
        best, n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
        if n * 2 > max(1, votes):
            if best:
                out[p] = best
        else:
            unresolved.append(p)          # 没共识 ≠ 不用转，必须区分
    return out, sorted(unresolved)


def recognize_quote_vl(file_path: str, *, vl_call: VLCall,
                       orient_call: OrientCall | None = None,
                       progress_cb=None, votes: int | None = None) -> ExtractionDraft:
    """生产入口：整份 PDF → ExtractionDraft。"""
    from PIL import Image

    s = get_settings()
    votes = votes if votes is not None else int(getattr(s, "QUOTE_ORIENT_VOTES", 3))
    page_count = DocumentLoader.get_page_count(file_path)
    pages = list(range(1, page_count + 1))

    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    # 分批渲染并及时释放：一次性渲全份会把内存峰值顶穿（53 页的文档实测存在）。
    _notify("渲染页面", 15)
    images: dict[int, bytes] = {}
    for start in range(0, len(pages), RENDER_BATCH):
        images.update(DocumentLoader.render_pages(file_path, pages[start:start + RENDER_BATCH]))

    rotations: dict[int, int] = {}
    unresolved: list[int] = []
    if orient_call is not None:
        _notify("方向预检", 30)
        rotations, unresolved = detect_rotations(images, orient_call, votes=votes)
        for p, deg in rotations.items():
            with Image.open(io.BytesIO(images[p])) as im:
                buf = io.BytesIO()
                im.convert("RGB").rotate(-deg, expand=True).save(buf, "PNG")
                images[p] = buf.getvalue()

    _notify("识别报价清单", 55)
    text = vl_call([images[p] for p in pages if p in images], PROMPT_QUOTE_CSV)

    # 封面元信息（声明总价等）：复用已渲染的前两页，不重新渲染。
    _notify("读取封面信息", 80)
    meta = extract_quote_meta(
        [images[p] for p in range(1, QUOTE_META_PAGES + 1) if p in images], vl_call)

    _notify("整理结果", 90)
    draft = build_draft(text, file_path=file_path, page_count=page_count,
                        processed_pages=[p for p in pages if p in images],
                        rotations=rotations, unresolved_pages=unresolved,
                        supplier_name=meta.get("supplier_name") or "",
                        declared_total=meta.get("bid_total"))
    draft.meta["quote_meta"] = meta
    return draft
