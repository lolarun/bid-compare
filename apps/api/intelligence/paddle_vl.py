"""paddle_vl.py — PaddleOCR-VL 报价识别适配器（docs/design/26 §5，轨 P1）。

## 核心思路

百度 PaddleOCR-VL 的原生输出（整份 PDF → 异步任务 → 结构化 JSON：
`pages[].tables[].{cells[], matrix[]}`）跟 `vl_quote.py` 的 `VLCall` 契约
（`images: list[bytes] → CSV 文本`）形状完全不同——不是换个 provider 实现就能
接进 `recognize_quote_vl`，需要一条独立的顶层编排（跟 `tender_text_layer.py`
是同一种模式：文档级产物 → 拼装成 CSV → 复用现有 `build_draft()`）。

**适配器把 Paddle 的 `cells` 矩阵序列化成规范 CSV，喂给已有的 `build_draft()`**——
`parse_csv`、四道门、质量分级、行数台账全部照旧跑，字段级校验零成本继承，
"评测说的"和"生产执行的"零漂移（跟 design/24 dry-run 同一个零漂移论证）。

## 已知缺陷与修法（design/26 §3.3，本模块修复）

实测（凯硕新正报价表，20 列表头）发现两层缺陷，不是同一个：

1. **表格级**：表头文字从"税额"往后整体错位——真实数据在"单价含税"（表头
   完全没印这个词）、"价税合计"（表头误标成"品牌"）、"品牌"（表头误标成
   "备注"）这几个位置。这一层是**整张表格统一**的，修法是税额列之后按算术
   关系正着认列。
2. **逐行级，本模块最初漏判、后来补上的**：Paddle 对"空单元格"的处理不一致——
   某一行"材质"五个细分类子列里若有一个是空的，`matrix` 会直接**少一格**而
   不是补一个空字符串占位，导致这一行从空格之后的所有列相对表头整体左移
   一位。抽样比对过（seq=46 跟 seq=1 两行）：同一张表、同一份表头，一行对得
   齐、另一行整段错位——**证实这是逐行、不是表格级**的问题，纯粹按表头位置
   做映射对不上。

两层缺陷叠加的后果是：任何"表格级固定列位置"的映射，只要某一行触发第 2 层，
就连带算错第 1 层已经修好的价格列。修法必须落到**逐行**：税率列的值形如
"NN%"，是这张表里少有的、不会跟其它字段值混淆的形状标记，用它做**每行独立
的锚点**（`_locate_tax_rate_idx`）——不管这一行前面掉没掉格，只要在行内扫到
"%"形状的cell，单价/合价/税率/税额/含税尾部这几列都按相对这个锚点的偏移量
重新定位，不再假设它们停在表头算出来的绝对下标上。

## 适配器实现的三条提示词规则替换（原来靠模型听指令，现在靠结构判据）

1. `row_type`：小计/合计/总计关键词，不依赖模型判断（`_classify_row_type`）。
2. `page`：Paddle 原生按页输出，直接取 `page_num`（0 起），不用模型自己数。
3. 副本编号：结构化检测整份行序列里的等长重复区块（`copy_detect.detect_copies`，
   两条识别路径共用，非 Paddle 专属）。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Callable

from apps.api.core.utils import parse_num, parse_rate
from apps.api.intelligence.copy_detect import detect_copies
from apps.api.intelligence.extraction_draft import ExtractionDraft
from apps.api.intelligence.vl_quote import build_draft, map_columns
from apps.api.services.ingestion.draft_integrity import (
    AMOUNT_NOT_QUOTED, AMOUNT_VALUE, classify_amount_cell,
)

log = logging.getLogger(__name__)

PARSER_MODE = "paddle_vl"

# 含税/不含税互推的相对容差。凯硕新正实测约 20 行算术校验，仅 2 行有小幅偏差
# （可能是 OCR 数字误读），其余全部吻合——2% 覆盖正常四舍五入噪声，不至于宽松到
# 把不相关的列也认进来。
_ARITH_TOL = 0.02

# 税率列的形状标记："13%"这种"数字+%"——整张表里几乎不会有别的字段长这样，
# 用来做逐行独立锚点（不依赖它停在表头算出来的绝对下标，见模块文档"已知缺陷"）。
_PCT_RE = re.compile(r"^\d+(\.\d+)?\s*%$")

_SUBTOTAL_KW = ("小计",)
# "合价" 是 2026-08-22 补的：`ingestion/list_rows.FOOTER_MARKERS` 里一直有它，这里
# 漏了——**同一个一字之差 design/32 已经在报价入库侧修过一次**（那边是 `含税合计`
# 有、`含税合价` 无），本模块是这套词表的第三份拷贝，于是又栽了一遍：凯硕的
# "含税合价（元）："被当成明细行入库，90 行 vs 参照 89 行的差额就是它。
# 本表比 `FOOTER_MARKERS` 窄是**有意的**：识别侧要区分 subtotal / total 两种 row_type，
# 而那张表把"小计"和"合计"混在一起，直接共用会让小计行被标成合计。加词时两边都要看。
_TOTAL_KW = ("合价", "合计", "总计", "总价")

# 判断"这一行是不是表头"用这几个槽位是否解析成数字——表头本身不该是数字。
_NUMERIC_HINT_SLOTS = ("qty", "tax_rate", "unit_price_excl_tax", "total_price_excl_tax")

# 报价表判据：表头里出现任一即认为是报价明细表，排除纯规格参考表（如凯硕封面页
# 那张全空的"附清单"占位表）。跟 score_paddleocr_vl.py 用的是同一组关键词
# （不针对任何一份具体文档，换一批新文档不用改）。
_QUOTE_TABLE_HINTS = ("单价", "合价", "合计", "数量")


def _resolve_matrix(table: dict) -> list[list[str]]:
    """`matrix` 存的是 `cells[]` 数组的下标，不是文字本身——这里解出真正的文字。
    误读过一次（把 matrix 当文字用），产出超过 100% 的假召回率，教训见 design/26 §3.2。"""
    matrix = table.get("matrix")
    cells = table.get("cells") or []
    if not matrix:
        return []
    out = []
    for row in matrix:
        texts = []
        for idx in row:
            if isinstance(idx, int) and 0 <= idx < len(cells):
                texts.append(str(cells[idx].get("text") or "").strip())
            else:
                texts.append("")
        out.append(texts)
    return out


def _looks_numeric(s: str) -> bool:
    return parse_num(s) is not None


def _looks_numeric_row(row: list[str], positions: list[int]) -> bool:
    vals = [row[i] for i in positions if i < len(row) and row[i]]
    if not vals:
        return False
    return sum(1 for v in vals if _looks_numeric(v)) >= max(1, len(vals) // 2)


def _merge_header_rows(row0: list[str], row1: list[str], *, sep: str = "") -> list[str]:
    """两级表头（粗类目 + 细分类）拼成一行。凯硕的"材质"列在 row0 是同一个粗标签
    重复五次（阀体/阀芯/阀板/阀杆/密封圈这五个细分类共用一个粗类目），row1 才是
    真正区分列身份的文字——同类拼接手法见 `tender_text_layer._flatten_anchor_header`
    （轨A 招标清单也是同一种两级表头）。

    `sep`：报价侧无分隔符拼接（"单价"+"含税"→"单价含税"）；招标侧用下划线
    （"材质"+"阀体"→"材质_阀体"，`paddle_tender.py` 传入），跟 `TENDER_SLOTS`
    的材质收集逻辑（按下划线切分父子列名）和轨A `_flatten_anchor_header`
    的既有约定保持一致——两处独立实现，同一个约定，不是巧合。"""
    out = []
    for i in range(max(len(row0), len(row1))):
        a = (row0[i] if i < len(row0) else "").strip()
        b = (row1[i] if i < len(row1) else "").strip()
        if a and b and a != b:
            out.append(f"{a}{sep}{b}")
        else:
            out.append(b or a)
    return out


def _split_header_and_rows(
    grid: list[list[str]], *, slots: dict | None = None, sep: str = "",
) -> tuple[list[str], list[list[str]]]:
    """判断这张表是单行表头还是两行表头（粗类目+细分类），返回 (表头, 数据行)。

    `slots`/`sep`：默认走报价侧槽位表与无分隔符拼接；`paddle_tender.py` 传
    `TENDER_SLOTS`/`sep="_"` 复用同一套判断逻辑，不重写一遍（差异只在"用哪张
    槽位表识别数量列、父子列怎么拼"，两边约定必须一致才不会漂移）。"""
    if not grid:
        return [], []
    header0 = grid[0]
    if len(grid) < 2:
        return header0, []
    cmap0 = map_columns(header0, slots=slots)
    idx_of0 = {h: i for i, h in enumerate(header0)}
    hint_slots = _NUMERIC_HINT_SLOTS if slots is None else ("qty",)
    positions = [idx_of0[cmap0[s]] for s in hint_slots if s in cmap0 and cmap0[s] in idx_of0]
    if positions and not _looks_numeric_row(grid[1], positions):
        return _merge_header_rows(header0, grid[1], sep=sep), grid[2:]
    return header0, grid[1:]


def _looks_like_wrap_continuation(prev_row: list[str], row: list[str],
                                  name_idx: int, spec_idx: int | None) -> bool:
    """`row` 是不是 `prev_row` 名称跨行换行被拆出来的续行，不是独立的一条数据。

    实测两种形状（宏胜"预分支电缆头"复现，都是同一个根因——Paddle 把单元格
    内的物理换行拆成了两条 matrix 行）：
    1. 数值列被整段复制：续行 name 列之后所有列跟上一行逐位相等。
    2. 数值列整段清空、只有 spec 位留了一小段文字：那是被截断文本的尾巴
       （"...4X150+E" 续到下一行变成"70"），其余列全空。

    两种形状共同的判据：**除了 name/spec 两列，其余列要么跟上一行相等、
    要么是空**——真正独立的一行不可能在数量/单价/合价这些字段上跟前一行
    完全重合或干脆没有任何自己的数据。name/spec 列的具体下标由调用方按这张
    表实际的表头传入，不假定固定在 0/1（不同文档的列序不一样）。"""
    skip = {name_idx} | ({spec_idx} if spec_idx is not None else set())
    for i in range(max(len(prev_row), len(row))):
        if i in skip:
            continue
        a = (prev_row[i] if i < len(prev_row) else "") or ""
        b = (row[i] if i < len(row) else "") or ""
        if b.strip() and b.strip() != a.strip():
            return False
    return True


def _merge_wrapped_rows(data_rows: list[list[str]], name_idx: int,
                        spec_idx: int | None) -> list[list[str]]:
    """把"名称跨行换行被拆成两条 matrix 行"的续行折回上一行，返回过滤/合并后
    的数据行列表。name 列片段拼接；spec 列只在续行有**新内容**（不是上一行的
    原样重复）时才拼接——`_looks_like_wrap_continuation` 允许 spec 位不参与
    "其余列"的相等/空校验，正是因为这一位是唯一可能带续行内容的列，合并时
    要单独处理，不能跟其余列一视同仁地要求"相等或空"。"""
    out: list[list[str]] = []
    for row in data_rows:
        if out and _looks_like_wrap_continuation(out[-1], row, name_idx, spec_idx):
            prev = out[-1]
            merged = list(prev)
            r_name = (row[name_idx] if name_idx < len(row) else "") or ""
            p_name = (prev[name_idx] if name_idx < len(prev) else "") or ""
            if name_idx >= len(merged):
                merged += [""] * (name_idx + 1 - len(merged))
            merged[name_idx] = p_name + r_name
            if spec_idx is not None:
                r_spec = (row[spec_idx] if spec_idx < len(row) else "") or ""
                p_spec = (prev[spec_idx] if spec_idx < len(prev) else "") or ""
                if r_spec.strip() and r_spec.strip() != p_spec.strip():
                    if spec_idx >= len(merged):
                        merged += [""] * (spec_idx + 1 - len(merged))
                    merged[spec_idx] = p_spec + r_spec
            out[-1] = merged
        else:
            out.append(row)
    return out


# 必须是数值的槽位。**税率不在内**：它的合法字面值是 "13%"，`parse_num` 不认百分号
# （见 `_parse_rate` 存在的理由），放进来会让每一个正常行都被判成脏行。
_AMOUNT_SLOTS = ("qty", "unit_price", "total_price",
                 "unit_price_excl_tax", "total_price_excl_tax",
                 "unit_price_incl_tax", "total_price_incl_tax", "tax_amount")

# 合价类槽位，从左到右就是它们在真实表里的惯常顺序（不含税 → 通用 → 含税）。
# `_recover_shifted_total` 拿最左边那个当"位移锚点"：位移是整体的，认最左边一个
# 就够，认多了反而会在同一行里挑出互相矛盾的两个来源。
_TOTAL_SLOTS = ("total_price_excl_tax", "total_price", "total_price_incl_tax")


def _dirty_amount_slots(fields: dict) -> list[str]:
    """数值槽位里装着自由文本（既不是数，也不是"/"、"无"这类明确不报价标记）的槽位名。

    **这是"这一行的位置映射已经坏了"的证据，不是"这一格没读到"。** 远东实测：某行
    少了一格，单位之后整体左移一位，备注的自由文本落进 `total_price` 槽位；
    `classify_amount_cell` 判它非数字返回 `AMOUNT_EMPTY`、下游把它变成 None，
    **唯一能证明这行坏了的证据就此被静默丢弃**，而旁边两个同样移了位的数字
    （真实身份是单价和合价）看着完全正常地存了进去——31/138 行数量是错值、
    `validation_flags` 全空。空值是诚实状态，形状合理的错数字不是。

    既有的两道闸门都看不见这件事：`check_column_alignment` 跑在已经规范化成统一
    宽度的 CSV 上；`_has_plausible_numeric_signal` 只要有**一个**数值槽能解析就放行
    （移位后 qty/单价恰好都是数字）。详见 docs/design/34。
    """
    out = []
    for slot in _AMOUNT_SLOTS:
        v = (fields.get(slot) or "").strip()
        if v and parse_num(v) is None and classify_amount_cell(v) != AMOUNT_NOT_QUOTED:
            out.append(slot)
    return out


def _row_arithmetic_consistent(fields: dict) -> bool:
    """这一行剩下的金额之间还能不能自圆其说——用来决定脏行要清掉多少。

    脏槽位证明发生了移位，但**移位不一定波及整行**：泰科龙/凯硕实测有 6+1 行是
    单位落进了数量槽，价格三件套完全自洽——把这些正确价格一起清掉是矫枉过正。
    远东那类则相反：qty 和单价都是别行的数字，一条恒等式也立不住。

    判据本身搬去了 `draft_integrity.row_identities_hold`（2026-08-23，实现
    design/33 时）——空格子补位要用**同一把尺子**判填充可不可信，两处各写一份
    迟早会漂。这里保留薄封装，是因为识别阶段的容差比入库门宽（见
    `_ARITH_TOL` / `EXTRACTION_ARITHMETIC_TOLERANCE` 的注释），那个差异是有意的。
    """
    from apps.api.services.ingestion.draft_integrity import row_identities_hold

    return row_identities_hold(fields, tolerance=_ARITH_TOL)


def _recover_shifted_total(col_map: dict[int, str], row: list[str],
                           prev_row: list[str] | None = None) -> tuple[str, str] | None:
    """位移行里**只把合价**救回来，返回 (槽位名, 值)；救不了返回 None。

    为什么单独救合价、不顺手救数量和单价（远东实测逐值核对过）：位移是"某一格被
    Paddle 丢掉、右边所有列整体左移一位"。**丢掉那一格右边的值都还在，只是错位；
    它左边的没动；唯独它本身没了。** 备注是自由文本，它落在哪一格就暴露了位移量
    （跟 `_locate_tax_rate_idx` 用"NN%"的形状当逐行锚点是同一个套路，不是新判据）。
    合价紧邻备注、在位移点右侧，右移回去必对；数量/单价在位移点附近，右移回去拿到
    的是被纵向游程平滑污染的邻行值——实测某行右移后合价 1666013.63 正确，而单价会
    变成 150009.49，那是另一行的合价。所以这里**只动合价**。

    只在恰好一个金额槽位是自由文本时才敢做：多个脏槽位说明这一行乱得不止一处位移，
    位移量算不准，宁可整行留空（design/34 §3 的"拒绝优先"仍然是默认）。
    """
    dirty = [(i, slot) for i, slot in col_map.items()
             if slot in _AMOUNT_SLOTS
             and (v := (row[i] if i < len(row) else "").strip())
             and parse_num(v) is None and classify_amount_cell(v) != AMOUNT_NOT_QUOTED]
    if len(dirty) != 1:
        return None
    d_idx = dirty[0][0]
    # **只认一种位移形态：自由文本落在"整表最右列的左边一格"，即位移恰好 1。**
    #
    # 曾经写成"取右边最近的非金额列当它的家"，那是错的，7 份实测里对了 26/29 是运气：
    # `tax_rate` 不在 `_AMOUNT_SLOTS`（"13%" 解析不出数，见 `_parse_rate`），会被当成
    # 文本列选成"家"；`brand` 和 `remark` 都是自由文本，选哪个没有依据；某份实测还
    # 算出位移 3——一行掉三格几乎不可能，那次就救错了。
    #
    # 收紧到"最右列左邻 + 位移 1"之后实测：救回从 29 次降到 22 次，而**各份合价命中
    # 标答的数量一个都没少**（泰科龙 80、凯硕 87 不变）——被砍掉的 7 次恢复，那些行的
    # 金额本来就已被 `_row_arithmetic_consistent` 保住，恢复是多余动作。同样的准确率、
    # 少一堆没道理的猜测，这是收紧的全部理由。
    rightmost = max(col_map)
    if rightmost - d_idx != 1:
        return None
    shift = 1
    tot = next(((i, slot) for i, slot in sorted(col_map.items()) if slot in _TOTAL_SLOTS), None)
    if tot is None:
        return None
    src = tot[0] - shift
    if not (0 <= src < len(row)):
        return None
    v = (row[src] or "").strip()
    if parse_num(v) is None:
        return None
    # 纵向游程平滑的护栏：同一个值在相邻行的**同一格**里重复出现，说明这一格是被
    # 上一行涂下来的，不是这一行自己的数（远东实测 `150009.49` 连续三行占着同一格）。
    # 救回一个被污染的合价比留空更糟——留空看得见，错值看不见。
    if prev_row is not None and src < len(prev_row) and (prev_row[src] or "").strip() == v:
        return None
    return (tot[1], v)


def _classify_columns(header: list[str]) -> dict[int, str]:
    """列 → 槽位，只按表头文字（`map_columns`，跟 qwen 路径同一套关键词表）。
    只给"材质区块之前"稳定不受行级掉格影响的字段（目前是 name/spec）当权威
    来源，也给找不到税率锚点的异常行当退化兜底——数量/单价/税率/税额/含税
    尾部这些字段的**主路径**改由 `_extract_row_fields` 逐行按 `_locate_tax_rate_idx`
    重新定位，不再信任这里算出来的绝对下标（design/26 §3.3 的行级缺陷）。"""
    base = map_columns(header)
    idx_of = {h: i for i, h in enumerate(header)}
    return {idx_of[h]: slot for slot, h in base.items() if h in idx_of}


def _locate_tax_rate_idx(row: list[str]) -> int | None:
    """在行内容里找税率列（"13%"这种形状），不信任表头算出来的绝对下标——
    这一行前面只要有一个空单元格被 Paddle 悄悄压缩掉，绝对下标就会整体偏移。
    命中不止一个时判定不可信（正常表格税率只有一列），交回调用方走退化路径。"""
    hits = [i for i, c in enumerate(row) if c and _PCT_RE.match(c.strip())]
    return hits[0] if len(hits) == 1 else None


def _parse_rate(s: str) -> float | None:
    """税率归一化。**实现已收拢到 `core.utils.parse_rate`**（2026-08-23）。

    这里原本是本模块私有的一份，注释里留着一个未决问题：「另行核实是否也该在
    别处修」。答案是该——`tabular_ingestion` 读 Excel 的税率列时撞上了同一件事
    （Excel 常把 13% 存成裸数字 13），于是两份实现变成必然。保留这个薄封装只
    为不动本模块既有调用点的名字。"""
    return parse_rate(s)


def _classify_trailing_cells(excl_unit: str, excl_total: str, rate: str,
                             tax_amount: str, trailing: list[str]) -> dict[str, str]:
    """税额列之后的剩余列（含税单价/含税合计/品牌/备注，具体几列因行而异）逐行
    按算术关系正着认——不管原表头在这几个位置写的是什么字。

    数量为 1 的行，含税单价与含税合计数值相同，算术上无法从数值本身区分。
    这时候看这一行 trailing 区里有几个"数字形状"的候选：泰科龙实测（89 行
    price 表）这类表trailing 只有一个数字候选（该表压根没有单独的"含税单价"
    列，只报"价税合计"）——两个槽位都去抢这唯一的候选，"单价"先查先得就会
    抢到本该属于"合计"的值（golden 验证：qty=1 时 unit_price_incl_tax 应为
    None，total_price_incl_tax 才是那个数）。凯硕反例：trailing 有两个数字
    候选，两个槽位各自独立匹配到不同的候选，不存在这个歧义。
    候选数 ≤1 时只试合计、不试单价——单栏"价税合计"是发票惯例里更常见的
    单栏形态，没有独立证据（第二个数字候选）就不该猜多出一个"单价含税"。"""
    neu, net, nr, nam = (parse_num(excl_unit), parse_num(excl_total),
                         _parse_rate(rate), parse_num(tax_amount))
    expected_unit = neu * (1 + nr) if neu is not None and nr is not None else None
    expected_total = net + nam if net is not None and nam is not None else None

    remaining = list(enumerate(trailing))
    numeric_candidates = sum(1 for _, v in remaining if parse_num(v) is not None)

    def _take_closest(expected: float | None) -> int | None:
        if expected is None:
            return None
        best_i, best_diff = None, None
        for i, v in remaining:
            nv = parse_num(v)
            if nv is None:
                continue
            diff = abs(nv - expected)
            if diff <= max(abs(expected), 1.0) * _ARITH_TOL and (best_diff is None or diff < best_diff):
                best_i, best_diff = i, diff
        return best_i

    out: dict[str, str] = {}
    i_unit = _take_closest(expected_unit) if numeric_candidates >= 2 else None
    if i_unit is not None:
        out["unit_price_incl_tax"] = trailing[i_unit]
        remaining = [(i, v) for i, v in remaining if i != i_unit]
    i_total = _take_closest(expected_total)
    if i_total is not None:
        out["total_price_incl_tax"] = trailing[i_total]
        remaining = [(i, v) for i, v in remaining if i != i_total]

    # 剩下的列按左到右顺序给 brand 再给 remark；算不出算术关系的数字列不猜，
    # 留空——parse_csv 的 unmapped_numeric_columns 诊断会报出来，好过拿一个
    # 猜的槽位冒充确定的映射。
    fallback = ["brand", "remark"]
    for i, v in remaining:
        if not v or _looks_numeric(v):
            continue
        if fallback:
            out[fallback.pop(0)] = v
    return out


def _extract_row_fields(col_map: dict[int, str], row: list[str]) -> dict[str, str]:
    """逐行取字段值。seq/name/spec 都在"材质掉格区间"（材质阀体/阀芯/阀板/阀杆，
    实测下标 6-10）**左边**，不受那段行内偏移影响，按表头位置直接取；数量/
    单价/税率/税额/含税尾部这几个字段改用行内容锚点（税率的"NN%"形状）逐行
    重新定位，不管材质区块有没有因为空单元格被 Paddle 压缩掉一格。

    seq 必须**每一行**都尝试取（不能只在下面兜底分支里取）：曾经只有兜底分支
    （税率锚点找不到时）才顺带取 seq，主路径（找到锚点的大多数行）完全不填。
    后果是一份文档里只有少数行有 seq、大多数没有——这不是"这份文档没有序号列"
    （那种情况该整份都没有），是同一份文档内部不一致，导致下游 `e2e_diff` 的
    `use_content_align`（要求要么全部行都没 seq、要么走 seq 精确匹配）两头不
    讨好：走了精确匹配分支，却只匹配上那零星几行，recall 从两位数打到 10%
    （泰科龙实测复现）。生产侧同样受影响：`check_sequence_continuity`
    （vl_quote.build_draft）拿 seq 判"行数守恒"，seq 有一搭没一搭会让判据本身
    不可用。"""
    fields: dict[str, str] = {}
    for i, slot in col_map.items():
        if slot in ("seq", "name", "spec") and i < len(row) and row[i]:
            val = row[i]
            if slot in ("name", "spec"):
                val = _strip_wrap_escape(val)
            fields[slot] = val

    rate_idx = _locate_tax_rate_idx(row)
    if rate_idx is not None:
        def _at(offset: int) -> str:
            i = rate_idx + offset
            return row[i] if 0 <= i < len(row) else ""

        # 表头惯常列序：...单位,数量,单价不含税,合计不含税,税率(锚点),税额,尾部...
        unit, qty, excl_unit, excl_total, rate, tax_amount = (
            _at(-4), _at(-3), _at(-2), _at(-1), _at(0), _at(1))
        # 税率落 CSV 前转成小数文本（"13%"→"0.13"）——不是改这个事实（13% 数学上
        # 就是 0.13），是因为下游 build_quote_fields 复用同一个非 lenient
        # `_num`/parse_num，那个解析器不认百分号，原样落"13%"会让税率在
        # ExtractionDraft 里静默变 None（已实测确认）。PROMPT_QUOTE_META 对封面
        # 声明税率早就是同一个约定（"小数形式，如 13% 写 0.13"），这里对逐行
        # 税率保持一致，不是新发明的规则。
        rate_dec = _parse_rate(rate)
        for slot, val in (("unit", unit), ("qty", qty),
                         ("unit_price_excl_tax", excl_unit),
                         ("total_price_excl_tax", excl_total),
                         ("tax_rate", "" if rate_dec is None else str(rate_dec)),
                         ("tax_amount", tax_amount)):
            if val:
                fields[slot] = val
        trailing = row[rate_idx + 2:]
        fields.update(_classify_trailing_cells(excl_unit, excl_total, rate, tax_amount, trailing))
    else:
        # 找不到税率锚点（小计/合计行，或者这一行本身没有税率数据）——退回表头
        # 位置映射，好过完全没有数据；这类行下游会被 row_type/结构门另行处理，
        # 不冒充可信的价格数据。
        for i, slot in col_map.items():
            if slot not in fields and i < len(row) and row[i]:
                fields[slot] = row[i]
        # 退化路径下 tax_rate 若原样是百分号文本，同样要转小数——理由同上
        # （下游 _num 不认百分号）。
        if fields.get("tax_rate"):
            rate_dec = _parse_rate(fields["tax_rate"])
            fields["tax_rate"] = "" if rate_dec is None else str(rate_dec)
    return fields


def _classify_row_type(row: list[str], name_idx: int | None) -> str:
    """小计/合计/总计行：财务报表通用惯例——整行大半是空的，只有开头一两格写着
    这几个字，不针对任何一份具体文档的版式。"""
    text = (row[name_idx].strip() if name_idx is not None and name_idx < len(row) else "")
    if not text:
        non_empty = [c.strip() for c in row if c and c.strip()]
        text = non_empty[0] if 0 < len(non_empty) <= 2 else ""
    if any(kw in text for kw in _SUBTOTAL_KW):
        return "subtotal"
    if any(kw in text for kw in _TOTAL_KW):
        return "total"
    return "detail"


def _looks_like_quote_table(header: list[str]) -> bool:
    return any(kw in h for h in header for kw in _QUOTE_TABLE_HINTS)


# 数量/单价/合价这几个槽位——判"这一行到底像不像报价数据"用它们，不用 name
# （name 位一样会被续页误吸收的无关表格填上文字，靠它挡不住，亨通实测复现）。
_NUMERIC_FIELD_KEYS = ("qty", "unit_price_excl_tax", "total_price_excl_tax", "tax_amount",
                       "unit_price_incl_tax", "total_price_incl_tax", "unit_price", "total_price")


def _has_plausible_numeric_signal(fields: dict) -> bool:
    """这几个数值槽位一个都没填——正常（小计/合计行，或者这行确实没报价），
    不能因为空就拒。但凡填了字，至少要有一个能解析成数，或者是"/"、"无"这类
    明确的"不报价"标记（跟 draft_integrity.classify_amount_cell 同一套判据，
    不是本模块另起一套）——否则这些槽位上塞的是自由文本，说明这一行根本不是
    报价数据，是续页误把毫不相关的表格（亨通实测：附录里的"偏差说明"条款表）
    当成续页数据吃了进来：qty 位是"偏离"、"偏差说明"这种自由文本，不是任何
    合法的数量语义，而现有"name 或 qty 非空即收"的判据挡不住——那两个字段
    这里都是"非空"，只是内容驴唇不对马嘴。"""
    populated = [fields.get(k) for k in _NUMERIC_FIELD_KEYS if fields.get(k)]
    if not populated:
        return True
    return any(classify_amount_cell(v) in (AMOUNT_VALUE, AMOUNT_NOT_QUOTED) for v in populated)


# 全角/半角标点等价——"材料（设备）名称"（真表头，全角括号）跟"材料(设备)名称"
# （续页表头重复行，半角括号）字面不相等，但是同一个词。浦东电缆实测复现：不做
# 归一化，逐字匹配会直接漏判。只归一化标点，不归一化字母数字——不能把两个本来
#不同的词碰巧削成一样。
_PUNCT_NORMALIZE = str.maketrans("（）【】：，", "()[]:,")


def _normalize_label(s: str) -> str:
    return s.translate(_PUNCT_NORMALIZE).strip()


def _strip_wrap_escape(s: str) -> str:
    """去掉字面转义序列 "\\n"（反斜杠+n 两个字符，不是真换行）——Paddle 对
    跨行换行的单元格输出带这个转义序列（"预分支电缆头\\nRTTYZ-..."），内容
    其实是一个词，不剥离会在 name/spec 里留下肉眼可见的噪声（跟 e2e_diff.py
    的 `_norm_str` 同一个发现、同一个理由，这里是生产落值，那边是评分归一化，
    两处独立剥离，不是同一份代码）。"""
    return s.replace("\\n", "")


def _is_divider_row(row: list[str], header: list[str]) -> bool:
    """跳过"看着像数据、实际是装饰性分节/表头重复行"的行——不是 `_classify_row_type`
    的小计/合计（那些是真数据，只是汇总），是压根不该进清单的噪声。两种形状（浦东
    电缆实测复现）：

    1. 分节标题行：Paddle 把"矿物电缆:"这类分节标题单独切成一张"表"，同一段文字
       重复铺满整行每一列——不是数据，非空单元格几乎全相等就是这种标记。
    2. 表头重复行：续页把列标签文字本身（"材料(设备)名称,规格型号,..."）当数据行
       带了进来——续页没有自己的表头，`build_quote_csv` 沿用上一张真表头时，如果
       这张"表"本身只是"分节标题+表头重复"两行，两行都会被当数据吃进来。
       这里判据是行内容与当前表头逐位重合度高（标点归一化后比较）。

    两种形状都源于同一个上游动作（续页沿用上一份表头时把整张原始 grid 当数据，
    不再区分它是不是真的续页数据）——本函数只做最后一道过滤，不改动上游续页判定，
    避免影响真正的续页数据。"""
    vals = [c.strip() for c in row if c and c.strip()]
    if len(vals) >= 3 and len(set(vals)) == 1:
        return True  # 同一段文字铺满整行
    header_set = {_normalize_label(h) for h in header if h and h.strip()}
    if len(vals) >= 2 and header_set:
        hits = sum(1 for v in vals if _normalize_label(v) in header_set)
        if hits / len(vals) >= 0.8:
            return True  # 这一行几乎就是表头文字本身
    return False


# CSV 列顺序跟 PROMPT_QUOTE_CSV 的契约一致：row_type 第一列，copy_no 倒数第二列，
# page 最后一列（vl_quote.parse_csv 靠这几个位置的语义、不靠位置本身解析，但保持
# 同一约定方便人工读原始 CSV 时核对）。
_CANONICAL_SLOTS = [
    "seq", "name", "spec", "unit", "qty", "brand", "remark",
    "unit_price_excl_tax", "total_price_excl_tax", "tax_rate", "tax_amount",
    "unit_price_incl_tax", "total_price_incl_tax",
    # 通用价格槽位（单价/合价单据表，没有含税/不含税之分，实测浦东电缆、绵存
    # 两份都是这种版式）——漏掉这两列的话，_extract_row_fields 明明取到了值，
    # 却在这里序列化 CSV 时被整体丢弃（已实测复现：两份文档 recall 100% 但
    # 所有价格字段全空）。
    "unit_price", "total_price",
]


def _merged_page_spans(pages: list[dict]) -> dict[int, int]:
    """`begin` 页 → 这张跨页表最后一个续页的 `page_num`（0 起，跟 Paddle 一致）。

    Paddle 对跨页续表返回 `tables[].merge_table = begin|inner|end`，**整段行全部塞进
    begin 那一页的 table 对象**，inner/end 页只留一个 `cells=[]/matrix=[]` 的空壳。
    行一条不丢、顺序不乱，丢的是**页号**——续页上的行会全部继承 begin 页的页码。
    实测覆盖面：7 份 fixture 除一份外全部出现，3 次重跑逐字节一致（泰科龙 19/89 行
    错页、绵存一张表 73 行横跨 4 个物理页）。

    没有几何可以把行拆回物理页（Paddle 的 table `cells[].position` 全是 `None`），
    所以这里只算**跨页区间**，交给下游如实标注"第 N-M 页"，不再把 begin 页当事实
    断言——宁可说不准，不能说错（`.claude/rules/recognition.md` 来源诚实标注）。
    """
    spans: dict[int, int] = {}
    begin: int | None = None
    for page in pages:
        num = page.get("page_num")
        if not isinstance(num, int):
            continue
        for table in page.get("tables") or []:
            state = table.get("merge_table")
            if state == "begin":
                begin = num
                spans[begin] = num
            elif state in ("inner", "end") and begin is not None:
                spans[begin] = num
                if state == "end":
                    begin = None
            elif state is None:
                # 普通独立表：中断当前跨页链，避免把后面无关的表算进区间。
                begin = None
    return spans


def build_quote_csv(doc_json: dict) -> str | None:
    """Paddle 结构化 JSON → 规范 CSV 文本。一份文档没有任何可辨认报价表时返回
    None（交给调用方判定 BLOCKED，不产出一个空壳 CSV 让下游误以为"已尝试且无货"）。
    """
    pages = doc_json.get("pages") or []
    page_spans = _merged_page_spans(pages)
    last_header: list[str] | None = None
    # 续页续接必须限定在**相邻页范围内**（同一份的判据，见 tender_text_layer.py
    # 的 build_anchor_csv 同款先例）。
    #
    # **这个上限原来的依据已经失效，2026-08-22 重新取证。** 原注释写的是"泰科龙
    # 报价表内部偶有跳页，最大间隔 2 页"——那些跳页是 `merge_tables=True` 造出来的
    # 假象（Paddle 把跨页续表整段塞进 begin 页，inner/end 页留空壳），本仓库当天已把
    # 该参数默认值改成 False。改完之后 7 份实测里 6 份的报价表页码**完全连续**，
    # 只有一份仍有一处间隔 2 页（尾页与前一段之间夹了一页非清单内容）。
    # 上限仍然必须存在，但依据换成了后者。
    #
    # 不设上限的后果同样是实测过的：文档后段的尺寸/材料参考表跟报价表结构无关、
    # 恰好又没有报价关键词而被判成"非表头"，会被当成续页一路吃到文档末尾——
    # 某份 188 行里 99 行是这么混进来的假续页，recall 从可用打到 12.4%。
    _MAX_CONTINUATION_GAP = 3
    last_price_page: int | None = None
    collected: list[dict] = []  # 每行：{slot: text, ..., "_page": int, "_row_type": str}

    for page in pages:
        page_num = page.get("page_num")
        page_1based = (page_num + 1) if isinstance(page_num, int) else None
        # 跨页合并表：这一页的行其实横跨 page_1based..page_end_1based，见 _merged_page_spans。
        page_end_1based = page_1based
        if isinstance(page_num, int) and page_num in page_spans:
            page_end_1based = page_spans[page_num] + 1
        for table in page.get("tables") or []:
            grid = _resolve_matrix(table)
            if len(grid) < 1:
                continue
            header, data_rows = _split_header_and_rows(grid)
            is_quote_header = _looks_like_quote_table(header)
            in_gap = (last_price_page is not None and isinstance(page_num, int)
                     and page_num - last_price_page <= _MAX_CONTINUATION_GAP)
            # 续页候选表的实际列数不能比上一张报价表的表头窄太多——真正的续页
            # 数据行宽度跟表头基本一致（偶有 ±1 的行级噪声，见模块文档"已知
            # 缺陷"）；宏胜/亨通实测复现：附录里的"招标文件条目号/偏离说明"
            # 条款表只有 4 列，价格表表头有 8-19 列，同样没有报价关键词、同样
            # 在相邻页范围内，会被当成续页吃进来。阈值取严格大于一半——宏胜
            # 实测 4/8 恰好卡在"不超过一半"的边界上，`>=` 会放行，必须用 `>`。
            # 真续页的列数噪声观测到的最大情况也就一两列，这个阈值不会误伤。
            width_plausible = (not last_header
                               or max((len(r) for r in grid), default=0) > len(last_header) / 2)
            if is_quote_header:
                last_header = header
                last_price_page = page_num if isinstance(page_num, int) else last_price_page
            elif last_header is not None and in_gap and width_plausible:
                # 续页没有自己的表头行——沿用同一份文档上一次成功识别的表头**文字**。
                # 列映射不再需要按续页宽度重算：字段值改由 `_extract_row_fields` 逐行
                # 按税率锚点重新定位，天然不依赖表头页跟续页的列数是否一致
                # （凯硕实测表头页 20 列、续页 21 列）。
                header, data_rows = last_header, grid  # 续页没有表头占位行，整张表都是数据
                last_price_page = page_num if isinstance(page_num, int) else last_price_page
            else:
                # 还没见过有效表头，或者跟上一张报价表隔太远——大概率是规格参考表。
                # 超出间隔就**清空** last_header，不只是跳过这一张：防止后面偶然
                # 出现的下一张不相关表格被当成"隔了很远也算续页"继续吃进来。
                if last_header is not None and not in_gap:
                    last_header = None
                continue
            col_map = _classify_columns(header)

            # name 列下标：col_map 是 idx->slot，找 slot=="name" 的那个下标——
            # 用来判定小计/合计/总计行（那几行通常只在 name 列写字，其余列留空）。
            name_idx = next((i for i, s in col_map.items() if s == "name"), None)
            spec_idx = next((i for i, s in col_map.items() if s == "spec"), None)
            if name_idx is not None:
                # 名称跨行换行被 Paddle 拆成两条 matrix 行（"预分支"/"电缆头"
                # 分别成行，实测宏胜复现）——先折回一行，再进入逐行抽取，否则
                # 这两条碎片各自当成一条脏行处理，name/spec 都不完整。
                data_rows = _merge_wrapped_rows(data_rows, name_idx, spec_idx)

            prev_raw_row: list[str] | None = None
            for row in data_rows:
                if not any((c or "").strip() for c in row):
                    continue  # 全空行（合并单元格续行的占位符）
                if _is_divider_row(row, header):
                    continue  # 分节标题/表头重复行——不是清单数据
                fields = _extract_row_fields(col_map, row)
                if not fields.get("name") and not fields.get("qty"):
                    continue  # 关键字段都拿不到，大概率是脏行
                if not _has_plausible_numeric_signal(fields):
                    continue  # 数值槽位塞的是自由文本——多半是续页误吸收了不相关表格
                # design/34：数值槽位里的自由文本 = 这一行位置映射已坏。**拒绝，不纠正**
                # ——远东实测那些行的真实数量/单价在整份响应全文里都搜不到，"往右移
                # 一位"只是给错值换个标签。脏槽位一律清空；其余金额看算术还立不立得住
                # （只有单位落进数量槽这种局部移位才留得下正确价格）。
                shifted_slots = _dirty_amount_slots(fields)
                recovered = None
                if shifted_slots:
                    # 清空之前先看合价救不救得回来——它的值往往还在行里，只是错位
                    # （design/34 §2.5 修正：数量/单价确实丢了，合价没丢）。
                    recovered = _recover_shifted_total(col_map, row, prev_raw_row)
                    for slot in shifted_slots:
                        fields[slot] = ""
                    if not _row_arithmetic_consistent(fields):
                        for slot in _AMOUNT_SLOTS:
                            fields[slot] = ""
                    if recovered is not None:
                        fields[recovered[0]] = recovered[1]
                # 未被槽位认领的原始列（专业/型号/工作压力/材质×）**不**在这里
                # 自行保留成额外 CSV 列：曾经这样做过，后果是原始中文表头文字
                # （比如"型号"）会在 CSV 回灌进 `parse_csv` 时被它自己的
                # `map_columns` 二次解析，跟 `_SLOTS["spec"]` 的 `("型号",)` 这个
                # tier 撞上，把已经从"规格"列正确取到的 spec 值顶替掉（泰科龙实测
                # 复现：spec 被换成型号值，qty/price 全线跟着错位）。`parse_csv`
                # 自己就有 unclaimed-column → extra_fields 的机制（`extra=`那行），
                # 不需要在这里重复一遍还留一个撞车的口子。
                prev_raw_row = row
                row_type = _classify_row_type(row, name_idx)
                collected.append({
                    **fields,
                    "_page": page_1based,
                    "_page_end": page_end_1based,
                    # 值 "recovered" 与 "1" 的区别下游要看得见：前者的合价是**按位移
                    # 锚点推回来的**，不是直接读到的，界面和复核都该知道这件事。
                    "_column_shift": ("recovered" if recovered else "1") if shifted_slots else "",
                    "_row_type": row_type,
                })

    if not collected:
        return None

    row_keys = [(r.get("name", ""), r.get("spec", ""), r.get("unit", ""), r.get("qty", ""))
               for r in collected]
    copy_nos = detect_copies(row_keys)

    fieldnames = (["row_type"] + _CANONICAL_SLOTS
                  + ["copy_no", "page", "page_end", "column_shift"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(fieldnames)
    for r, copy_no in zip(collected, copy_nos):
        row_out = [r["_row_type"]] + [r.get(s, "") for s in _CANONICAL_SLOTS]
        row_out += [str(copy_no), str(r["_page"] or ""), str(r.get("_page_end") or ""),
                    r.get("_column_shift") or ""]
        writer.writerow(row_out)
    return buf.getvalue()


# ─── 生产入口（provider 编排，§P1）──────────────────────────────────────────
# 提交/轮询逻辑本身可注入，测试不需要网络（跟 vl_quote.py 的 VLCall 同一约定）。
# design/27 §6 补：轮询期间需要报进度（page_count/progress_cb），签名放宽成
# Any-arity（跟 pipeline.py 的 ProgressCallback 同一个理由：关键字参数按需
# 传，不强行把每个调用点的类型标注都写死）。
SubmitAndParse = Callable[..., dict]


def recognize_quote_paddle(file_path: str, *, submit_and_parse: SubmitAndParse,
                           page_count: int, supplier_name: str = "",
                           declared_total: float | None = None,
                           text_call=None, requirements=None,
                           gap_filler=None,
                           progress_cb=None) -> ExtractionDraft:
    """生产入口：整份 PDF → Paddle 结构化 JSON → 规范 CSV → ExtractionDraft。

    `submit_and_parse` 是 Paddle 提交/轮询/下载解析结果的完整实现（生产侧用
    `scripts/try_paddleocr_vl.py` 同款百度云调用，见该脚本 `run_one` 的实现），
    这里不重复内嵌网络调用——保持本模块可离线单测（`.claude/rules/recognition.md`
    可测试性要求）。封面声明总价（`declared_total`）走表格识别管不到——那是
    自由文本问答，不是结构化表格；declared_total 检验门在没有这个输入时按
    unknown 处理，不阻断，跟轨A的 `parse_tender_document_text_layer` 对封面
    meta 缺失时的处理是同一个先例。

    `text_call`/`requirements`（design/26 P4 补，2026-08-13）：投标文件跟招标
    文件一样支持声明式要求抽取（用户明确要求），喂 Paddle 已经 OCR 出来的每页
    文字，不需要再发一次 vision 调用——见 `paddle_doc_meta.py`。`text_call` 为
    None（未配置文字抽取客户端）时要求整体留空，不阻断报价清单——清单才是
    主线。

    阶段命名遵循 design/27 §6：不带引擎术语（"提交 PaddleOCR-VL 识别"这类
    名字被禁），四段用户可读命名里本函数负责后三段——①上传由前端字节进度
    覆盖，不在这里。"识别内容"是唯一的长阶段（20-90s 量级），没有逐页信号，
    只能靠"已耗时÷预计耗时"估算，估算封顶
    `domain_config.PADDLE_PROGRESS_ESTIMATE_CAP`（不能在真正完成前显示100%）。
    """
    from apps.api.core.domain_config import (
        PADDLE_EXPECTED_SECONDS_PER_PAGE, PADDLE_PROGRESS_ESTIMATE_CAP,
    )

    def _notify(stage: str, pct: int, *, stage_current: int | None = None,
               stage_total: int | None = None) -> None:
        if progress_cb:
            progress_cb(stage, pct, stage_current=stage_current, stage_total=stage_total)

    expected_s = PADDLE_EXPECTED_SECONDS_PER_PAGE * page_count if page_count else None

    def _poll_progress(elapsed_s: float, poll_expected_s: float | None) -> None:
        # `poll_expected_s` 是 submit_and_parse 自己算的（它也拿到了 page_count），
        # 跟这里的 expected_s 应该是同一个值——用它而不是外层闭包变量，保持
        # "谁计算、谁负责传出来"，不要求两处常量引用刚好同步。
        if poll_expected_s:
            frac = min(elapsed_s / poll_expected_s, PADDLE_PROGRESS_ESTIMATE_CAP)
            pct = 20 + int(70 * frac)
        else:
            # 没有页数估算基准（page_count 未传）：只有已耗时可报，没有比例，
            # 百分比给个不上不下的居中值，不假装能算出更精确的数。
            pct = 55
        _notify("识别内容", pct, stage_current=int(elapsed_s),
               stage_total=int(poll_expected_s) if poll_expected_s else None)

    _notify("识别内容", 20, stage_current=0,
           stage_total=int(expected_s) if expected_s else None)
    doc_json = submit_and_parse(file_path, page_count=page_count, progress_cb=_poll_progress)

    if text_call is not None:
        from apps.api.intelligence.paddle_doc_meta import (
            DEFAULT_QUOTE_REQUIREMENTS, extract_quote_meta_from_text,
            extract_requirements_from_text,
        )
        from apps.api.intelligence.vl_quote import QUOTE_META_PAGES

        pages = doc_json.get("pages") or []
        page_text_by_num = {
            p.get("page_num"): (p.get("text") or "")
            for p in pages if isinstance(p.get("page_num"), int)
        }
        all_texts = [page_text_by_num[n] for n in sorted(page_text_by_num)]

        # 报价封面元信息（design/27 §7.1）：supplier_name/declared_total 等四项。
        # Paddle 切换后从没被抽过，声明总价核对门因此一直是 unknown/不阻断——
        # 这里补上，跟招标侧 parse_tender_document_paddle 同一个模式（复用同一次
        # submit_and_parse 结果的页文字，不重新调用 Paddle）。
        _notify("提取信息", 92)
        quote_meta = extract_quote_meta_from_text(all_texts[:QUOTE_META_PAGES], text_call)

        reqs = requirements if requirements is not None else DEFAULT_QUOTE_REQUIREMENTS
        quote_requirements = extract_requirements_from_text(all_texts, text_call, reqs) if reqs else {}
    else:
        quote_meta = None
        quote_requirements = {}

    _notify("整理完成", 97)
    csv_text = build_quote_csv(doc_json)
    if csv_text is None:
        # 没有任何一张报价表——按 CLAUDE.md §4 BLOCKED（无有效报价）处理，
        # 用一份空表交给 build_draft，让现有质量门给出 BLOCKED 而不是在这里
        # 提前抛异常吞掉诊断信息。
        csv_text = "row_type," + ",".join(_CANONICAL_SLOTS) + ",copy_no,page,page_end,column_shift\n"

    processed_pages = list(range(1, page_count + 1))
    # 抽取到的封面 meta 优先于调用方传入的 supplier_name/declared_total
    # （调用方目前从不传，默认空——跟 vl_quote.py 顶层编排 `meta.get(...) or
    # supplier_name` 同一个优先级：文档自己的事实优先，参数只是保留的兜底位）。
    effective_supplier_name = (quote_meta or {}).get("supplier_name") or supplier_name
    effective_declared_total = (quote_meta or {}).get("bid_total")
    if effective_declared_total is None:
        effective_declared_total = declared_total
    draft = build_draft(csv_text, file_path=file_path, page_count=page_count,
                        processed_pages=processed_pages,
                        supplier_name=effective_supplier_name,
                        declared_total=effective_declared_total,
                        parser_mode=PARSER_MODE)
    # ── 空格子补位（docs/design/33，2026-08-22 用户批准）─────────────────────
    # **默认关闭**：`gap_filler=None` 时 `fill_gaps` 直接返回，7 份快照的回放
    # 指标逐字节不变。生产由调用方显式注入，测试因此保持离线可复现。
    #
    # 放在 `build_draft` **之后**：找洞的判据是"这张表有这个列、而这一格什么都
    # 没读到"，需要成型的 `DraftRow.fields` 和 `source_ref.page` 才能判，raw CSV
    # 阶段两者都还没有。
    if gap_filler is not None:
        from apps.api.intelligence.gap_fill import fill_gaps
        from apps.api.intelligence.document_loader import DocumentLoader

        _notify("补读缺失金额", 98)

        def _render(page: int) -> bytes | None:
            return DocumentLoader.render_pages(file_path, [page]).get(page)

        report = fill_gaps(draft.rows, filler=gap_filler, render_page=_render)
        if report.outcomes:
            # 补位做了什么必须随行落进 meta——补过的值在界面上要跟直读的区分开，
            # 复核时也要能回答"这个数哪来的"。只写成功不写拒绝就是黑箱。
            draft.meta["gap_fill"] = report.to_dict()
            log.info("空格子补位：%s", report.to_dict())

    if quote_meta:
        draft.meta["quote_meta"] = quote_meta
    if quote_requirements:
        draft.meta["quote_requirements"] = quote_requirements
    return draft
