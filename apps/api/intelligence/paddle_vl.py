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

from apps.api.core.utils import parse_num
from apps.api.intelligence.copy_detect import detect_copies
from apps.api.intelligence.extraction_draft import ExtractionDraft
from apps.api.intelligence.vl_quote import build_draft, map_columns

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
_TOTAL_KW = ("合计", "总计", "总价")

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


def _merge_header_rows(row0: list[str], row1: list[str]) -> list[str]:
    """两级表头（粗类目 + 细分类）拼成一行。凯硕的"材质"列在 row0 是同一个粗标签
    重复五次（阀体/阀芯/阀板/阀杆/密封圈这五个细分类共用一个粗类目），row1 才是
    真正区分列身份的文字——同类拼接手法见 `tender_text_layer._flatten_anchor_header`
    （轨A 招标清单也是同一种两级表头）。"""
    out = []
    for i in range(max(len(row0), len(row1))):
        a = (row0[i] if i < len(row0) else "").strip()
        b = (row1[i] if i < len(row1) else "").strip()
        if a and b and a != b:
            out.append(f"{a}{b}")
        else:
            out.append(b or a)
    return out


def _split_header_and_rows(grid: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """判断这张表是单行表头还是两行表头（粗类目+细分类），返回 (表头, 数据行)。"""
    if not grid:
        return [], []
    header0 = grid[0]
    if len(grid) < 2:
        return header0, []
    cmap0 = map_columns(header0)
    idx_of0 = {h: i for i, h in enumerate(header0)}
    positions = [idx_of0[cmap0[s]] for s in _NUMERIC_HINT_SLOTS if s in cmap0 and cmap0[s] in idx_of0]
    if positions and not _looks_numeric_row(grid[1], positions):
        return _merge_header_rows(header0, grid[1]), grid[2:]
    return header0, grid[1:]


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
    """税率原文形如 "13%"——`core.utils.parse_num` 只剥离已知分隔符（逗号/货币
    符号），不认百分号，直接 float("13%") 会 ValueError 返回 None（已实测确认）。
    这里按百分号语义转小数，只在本模块内部用，不改 `parse_num` 的全局行为——
    那是四层共用的基础设施，改它的影响面超出本次 P1 范围，另行核实是否也该
    在 `vl_quote.build_quote_fields` 那层修（qwen 的报价行 tax_rate 走同一个
    非 lenient `_num` 调用，理论上有同样的问题，但那份文档从未在真实百分号
    文本下验证过，这里不代它下结论）。"""
    if not s:
        return None
    t = s.strip()
    if t.endswith("%"):
        n = parse_num(t[:-1])
        return (n / 100) if n is not None else None
    return parse_num(t)


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
            fields[slot] = row[i]

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


# 全角/半角标点等价——"材料（设备）名称"（真表头，全角括号）跟"材料(设备)名称"
# （续页表头重复行，半角括号）字面不相等，但是同一个词。浦东电缆实测复现：不做
# 归一化，逐字匹配会直接漏判。只归一化标点，不归一化字母数字——不能把两个本来
#不同的词碰巧削成一样。
_PUNCT_NORMALIZE = str.maketrans("（）【】：，", "()[]:,")


def _normalize_label(s: str) -> str:
    return s.translate(_PUNCT_NORMALIZE).strip()


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


def build_quote_csv(doc_json: dict) -> str | None:
    """Paddle 结构化 JSON → 规范 CSV 文本。一份文档没有任何可辨认报价表时返回
    None（交给调用方判定 BLOCKED，不产出一个空壳 CSV 让下游误以为"已尝试且无货"）。
    """
    pages = doc_json.get("pages") or []
    last_header: list[str] | None = None
    # 续页续接必须限定在**相邻页范围内**（同一份的判据，见 tender_text_layer.py
    # 的 build_anchor_csv 同款先例）——泰科龙实测：报价表本身第 4-14 页（0起页码
    # 3-13）内部偶有跳页（Paddle 没在每页都切出表格对象），最大间隔 2 页；不设
    # 上限的话，第 35 页起的阀门尺寸/零件材料参考表（跟报价表结构完全无关，只是
    # 恰好没有报价关键词、被判成"非表头"）会被当成报价表续页一路吃到文档末尾——
    # 188 行里 99 行是这么混进来的假续页，直接把 recall 从可用打到 12.4%。
    _MAX_CONTINUATION_GAP = 3
    last_price_page: int | None = None
    collected: list[dict] = []  # 每行：{slot: text, ..., "_page": int, "_row_type": str}

    for page in pages:
        page_num = page.get("page_num")
        page_1based = (page_num + 1) if isinstance(page_num, int) else None
        for table in page.get("tables") or []:
            grid = _resolve_matrix(table)
            if len(grid) < 1:
                continue
            header, data_rows = _split_header_and_rows(grid)
            is_quote_header = _looks_like_quote_table(header)
            in_gap = (last_price_page is not None and isinstance(page_num, int)
                     and page_num - last_price_page <= _MAX_CONTINUATION_GAP)
            if is_quote_header:
                last_header = header
                last_price_page = page_num if isinstance(page_num, int) else last_price_page
            elif last_header is not None and in_gap:
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

            for row in data_rows:
                if not any((c or "").strip() for c in row):
                    continue  # 全空行（合并单元格续行的占位符）
                if _is_divider_row(row, header):
                    continue  # 分节标题/表头重复行——不是清单数据
                fields = _extract_row_fields(col_map, row)
                if not fields.get("name") and not fields.get("qty"):
                    continue  # 关键字段都拿不到，大概率是脏行
                # 未被槽位认领的原始列（专业/型号/工作压力/材质×）**不**在这里
                # 自行保留成额外 CSV 列：曾经这样做过，后果是原始中文表头文字
                # （比如"型号"）会在 CSV 回灌进 `parse_csv` 时被它自己的
                # `map_columns` 二次解析，跟 `_SLOTS["spec"]` 的 `("型号",)` 这个
                # tier 撞上，把已经从"规格"列正确取到的 spec 值顶替掉（泰科龙实测
                # 复现：spec 被换成型号值，qty/price 全线跟着错位）。`parse_csv`
                # 自己就有 unclaimed-column → extra_fields 的机制（`extra=`那行），
                # 不需要在这里重复一遍还留一个撞车的口子。
                row_type = _classify_row_type(row, name_idx)
                collected.append({
                    **fields,
                    "_page": page_1based,
                    "_row_type": row_type,
                })

    if not collected:
        return None

    row_keys = [(r.get("name", ""), r.get("spec", ""), r.get("unit", ""), r.get("qty", ""))
               for r in collected]
    copy_nos = detect_copies(row_keys)

    fieldnames = (["row_type"] + _CANONICAL_SLOTS + ["copy_no", "page"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(fieldnames)
    for r, copy_no in zip(collected, copy_nos):
        row_out = [r["_row_type"]] + [r.get(s, "") for s in _CANONICAL_SLOTS]
        row_out += [str(copy_no), str(r["_page"] or "")]
        writer.writerow(row_out)
    return buf.getvalue()


# ─── 生产入口（provider 编排，§P1）──────────────────────────────────────────
# 提交/轮询逻辑本身可注入，测试不需要网络（跟 vl_quote.py 的 VLCall 同一约定）。
SubmitAndParse = Callable[[str], dict]


def recognize_quote_paddle(file_path: str, *, submit_and_parse: SubmitAndParse,
                           page_count: int, supplier_name: str = "",
                           declared_total: float | None = None,
                           progress_cb=None) -> ExtractionDraft:
    """生产入口：整份 PDF → Paddle 结构化 JSON → 规范 CSV → ExtractionDraft。

    `submit_and_parse` 是 Paddle 提交/轮询/下载解析结果的完整实现（生产侧用
    `scripts/try_paddleocr_vl.py` 同款百度云调用，见该脚本 `run_one` 的实现），
    这里不重复内嵌网络调用——保持本模块可离线单测（`.claude/rules/recognition.md`
    可测试性要求）。封面声明总价等标量暂不解析（Paddle 走结构化表格识别，不是
    自由文本问答，`vl_quote.extract_quote_meta` 那套提示词在这里不适用——
    declared_total 检验门在没有这个输入时按 unknown 处理，不阻断，跟轨A的
    `parse_tender_document_text_layer` 对封面 meta 缺失时的处理是同一个先例）。
    """
    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    _notify("提交 PaddleOCR-VL 识别", 20)
    doc_json = submit_and_parse(file_path)

    _notify("解析报价清单", 70)
    csv_text = build_quote_csv(doc_json)
    if csv_text is None:
        # 没有任何一张报价表——按 CLAUDE.md §4 BLOCKED（无有效报价）处理，
        # 用一份空表交给 build_draft，让现有质量门给出 BLOCKED 而不是在这里
        # 提前抛异常吞掉诊断信息。
        csv_text = "row_type," + ",".join(_CANONICAL_SLOTS) + ",copy_no,page\n"

    _notify("整理结果", 90)
    processed_pages = list(range(1, page_count + 1))
    draft = build_draft(csv_text, file_path=file_path, page_count=page_count,
                        processed_pages=processed_pages,
                        supplier_name=supplier_name, declared_total=declared_total,
                        parser_mode=PARSER_MODE)
    return draft
