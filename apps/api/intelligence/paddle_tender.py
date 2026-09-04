"""paddle_tender.py — PaddleOCR-VL 招标采购清单适配器（docs/design/26 P4 补，招标侧）。

design/26 的确定方案（`a1738c4`/`1953a80`）是"PaddleOCR-VL 是所有扫描件唯一
引擎，招标报价一视同仁"——首轮 P4 只落地了报价侧（`paddle_vl.py`），把招标侧
落了空档，是这一轮补上。跟报价侧同一个套路（`tender_text_layer.py` 相对
`vl_quote.py` 的关系是同一个先例）：Paddle 的 `cells` 矩阵序列化成规范 CSV，
喂给已有的 `build_tender_draft()`——结构门、质量分级、行数台账全部照旧跑。

## 跟报价侧表头识别的关键差异（实测金桥招标件复现，`outputs/baidu_unlimited_ocr/
tender_jinqiao.json`，零成本离线验证）

招标清单表头是**三行**：标题行（同一段项目全名铺满整行，逐列重复）+ 两级列
表头（父列"材质"跨 5 个子列 阀体/阀芯/阀板/阀杆/密封圈）。报价侧没有这道标题
行，`_split_header_and_rows` 假设 `grid[0]` 直接就是表头——这里要先剥掉标题行
（复用 `paddle_vl._is_divider_row` 同款"同一段文字铺满整行"判据）。父子表头
合并用下划线连接（"材质"+"阀体"→"材质_阀体"），不是报价侧的无分隔符拼接
（"单价"+"含税"→"单价含税"）——`TENDER_SLOTS` 的材质收集逻辑
（`vl_tender.build_tender_fields`）按下划线切分父子列名，`tender_text_layer.
_flatten_anchor_header`（轨A）已经是这个约定的先例，这里不重新发明。

## 材质子列一样会触发"空单元格被压缩"缺陷，单位/数量也要锚点重定位

原以为招标清单价格列是空的、没有报价侧那种行级错位问题——**实测证伪**：
金桥复现同一个缺陷长在材质子列区间（不是价格列）：5 个材质子列若某一行有
一格为空，`matrix` 少一格而不是补空占位，导致排在材质区块**之后**的"单位/
数量"整体左移一位（32 行数量整段丢失）。修法跟报价侧同一个套路——用税率
列的"NN%"形状做每行独立锚点，单位/数量按**相对锚点的偏移量**取值（偏移量
从表头本身算，见 `_header_rate_anchor_offsets`），不管材质区块有没有被压缩。
没有税率列的表头（不是所有招标清单都带）退回表头绝对下标，好过没有数据。

## 封面标量/招标要求不在本模块——`paddle_doc_meta.py`

采购清单是表格、逐行；封面标量（项目名称等）与招标要求（品牌等）是文档级
标量，散落在自由文字里，硬塞进同一次抽取会互相拖累（跟 `vl_tender.py`
"清单是表格、标量是几个文档级值"同一个理由）。本模块只出清单，标量/要求
走 `paddle_doc_meta.py`（用 Paddle 已经 OCR 出来的每页文字，不需要再发一次
vision 调用），两者独立编排、在 `pipeline.py` 里各自调用后拼进同一个
`TenderParseResult`。
"""
from __future__ import annotations

import csv
import io
import logging

from apps.api.core.utils import parse_num as _num_or_none
from apps.api.intelligence.copy_detect import detect_copies
from apps.api.intelligence.paddle_vl import (
    _is_divider_row,
    _locate_tax_rate_idx,
    _merge_wrapped_rows,
    _resolve_matrix,
    _split_header_and_rows,
    _strip_wrap_escape,
)
from apps.api.intelligence.vl_quote import map_columns
from apps.api.intelligence.vl_tender import TENDER_SLOTS, build_tender_draft

log = logging.getLogger(__name__)

PARSER_MODE = "paddle_vl"

# 判"这张表是不是采购清单"只认数量列——招标文件同一份里常有别的表也带"序号"
# 或者子串命中"规格"（比如"业主招标品牌要求"表里的"技术规格书"），实测金桥
# 复现：光看"序号/规格/名称"这类宽松关键词会把品牌要求表也当成清单续页吃进
# 来。数量列是清单区别于其它招标附表（品牌要求/商务条款/偏差说明）的通用
# 特征——任何一份招标采购清单都得报数量，跟 `TENDER_SLOTS["qty"]` 同一组
# 关键词，不针对任何一份具体文档。
_TENDER_TABLE_HINTS = ("数量", "工程量", "quantity")

_SUBTOTAL_KW = ("小计",)
_TOTAL_KW = ("合计", "总计")

# 续页续接的相邻页范围——跟报价侧 `paddle_vl.build_quote_csv` 同一个常量值、
# 同一个理由（防止隔了很远的不相关表格被当成续页一路吃到文档末尾）。
_MAX_CONTINUATION_GAP = 3


def _looks_like_tender_table(header: list[str]) -> bool:
    return any(kw in h for h in header for kw in _TENDER_TABLE_HINTS)


def _classify_row_type(row: list[str], name_idx: int | None) -> str:
    text = (row[name_idx].strip() if name_idx is not None and name_idx < len(row) else "")
    if not text:
        non_empty = [c.strip() for c in row if c and c.strip()]
        text = non_empty[0] if 0 < len(non_empty) <= 2 else ""
    if any(kw in text for kw in _SUBTOTAL_KW):
        return "subtotal"
    if any(kw in text for kw in _TOTAL_KW):
        return "total"
    return "detail"


def _classify_columns(header: list[str]) -> dict[int, str]:
    base = map_columns(header, slots=TENDER_SLOTS)
    idx_of = {h: i for i, h in enumerate(header)}
    return {idx_of[h]: slot for slot, h in base.items() if h in idx_of}


def _header_rate_anchor_offsets(header: list[str], col_map: dict[int, str]) -> dict[str, int]:
    """实测金桥复现的同一个"空单元格被 Paddle 压缩掉一格"缺陷（`paddle_vl.py`
    模块文档"已知缺陷"那条，报价侧长在价格列、这里长在材质子列区间）：某一行
    材质 5 个子列若有一个是空的，`matrix` 少一格而不是补空字符串占位，导致
    "单位/数量"这些排在材质区块**之后**的字段整体左移一位——按表头绝对下标
    取会取错（实测 seq 27-61 区间复现，32 行数量整段丢失）。

    修法跟报价侧同一个套路：用"税率"列的形状标记（"NN%"）做每行独立锚点，
    单位/数量按**相对锚点的偏移量**取值，不管材质区块有没有因为空单元格被
    压缩掉一格。偏移量从表头本身算（表头行不受行级压缩缺陷影响，永远完整），
    不硬编码——不同文档材质子列数量不一样，偏移量也会不一样。

    没有税率列的表头（不是所有招标清单都带）返回空字典，调用方退回表头绝对
    下标映射（跟报价侧的退化路径同一个约定）。"""
    rate_idx = next((i for i, c in enumerate(header) if c and "税率" in c), None)
    if rate_idx is None:
        return {}
    offsets: dict[str, int] = {}
    for i, slot in col_map.items():
        if slot in ("unit", "qty"):
            offsets[slot] = i - rate_idx
    return offsets


# CSV 列顺序：row_type 第一列，招标专有槽位在通用槽位之后，copy_no/page 收尾
# ——跟 `TENDER_SLOTS` 的键集合保持一致，`materials` 子列不在这里枚举（原始
# "材质_*" 表头原样透传，`build_tender_fields` 从 `raw_cells` 里按前缀扫，
# 不需要 CSV 固定列位）。
_CANONICAL_SLOTS = [
    "seq", "name", "spec", "model", "pressure", "profession",
    "unit", "qty", "brand", "remark",
]


def build_tender_csv(doc_json: dict) -> str | None:
    """Paddle 结构化 JSON → 招标清单规范 CSV 文本。没有任何可辨认清单表时返回
    None（交给调用方判定 BLOCKED，跟报价侧 `build_quote_csv` 同一个约定）。
    """
    pages = doc_json.get("pages") or []
    last_header: list[str] | None = None
    last_list_page: int | None = None
    collected: list[dict] = []
    extra_headers: list[str] = []  # 材质子列表头，按首次出现顺序收集，供 CSV 表头用

    for page in pages:
        page_num = page.get("page_num")
        page_1based = (page_num + 1) if isinstance(page_num, int) else None
        for table in page.get("tables") or []:
            grid = _resolve_matrix(table)
            if len(grid) < 1:
                continue
            # 标题行：同一段项目全名铺满整行——跟 `_is_divider_row` 判"分节标题"
            # 用的是同一个"非空单元格几乎全相等"判据，这里只在**表头候选行**上
            # 复用它做一次性剥离，不影响下面数据行自己的分节标题过滤。
            if grid and len(set(c.strip() for c in grid[0] if c and c.strip())) <= 1 and \
               sum(1 for c in grid[0] if c and c.strip()) >= 3:
                grid = grid[1:]
            if not grid:
                continue
            header, data_rows = _split_header_and_rows(grid, slots=TENDER_SLOTS, sep="_")
            is_list_header = _looks_like_tender_table(header)
            in_gap = (last_list_page is not None and isinstance(page_num, int)
                     and page_num - last_list_page <= _MAX_CONTINUATION_GAP)
            width_plausible = (not last_header
                               or max((len(r) for r in grid), default=0) > len(last_header) / 2)
            # 数量列可信性：招标文件里常有别的表（品牌要求/商务条款）恰好跟清单
            # 表宽度相近、页码相邻，仅凭 width_plausible 挡不住——实测复现：
            # "品牌要求"表 5 列，跟一张 5 列清单表宽度相等，会被当成续页吃进来。
            # 借用 `last_header` 里数量列的位置，看候选表第一行这个位置像不像
            # 数字（空也算合理，可能这行没报数量）——非空且解析不出数字，
            # 说明这张表的列语义跟清单对不上，不是真续页（跟 paddle_vl.py
            # `_has_plausible_numeric_signal` 同一个"数值槽位塞自由文本就不是
            # 这类数据"判据，这里在表级别用同一个信号）。
            qty_plausible = True
            if last_header is not None and not is_list_header and grid:
                last_col_map = _classify_columns(last_header)
                qty_idx = next((i for i, s in last_col_map.items() if s == "qty"), None)
                if qty_idx is not None and qty_idx < len(grid[0]):
                    v = (grid[0][qty_idx] or "").strip()
                    if v and _num_or_none(v) is None:
                        qty_plausible = False
            if is_list_header:
                last_header = header
                last_list_page = page_num if isinstance(page_num, int) else last_list_page
                for h in header:
                    if h and h not in extra_headers:
                        extra_headers.append(h)
            elif last_header is not None and in_gap and width_plausible and qty_plausible:
                header, data_rows = last_header, grid
                last_list_page = page_num if isinstance(page_num, int) else last_list_page
            else:
                if last_header is not None and not in_gap:
                    last_header = None
                continue
            col_map = _classify_columns(header)
            name_idx = next((i for i, s in col_map.items() if s == "name"), None)
            spec_idx = next((i for i, s in col_map.items() if s == "spec"), None)
            if name_idx is not None:
                data_rows = _merge_wrapped_rows(data_rows, name_idx, spec_idx)
            rate_offsets = _header_rate_anchor_offsets(header, col_map)

            for row in data_rows:
                if not any((c or "").strip() for c in row):
                    continue
                if _is_divider_row(row, header):
                    continue
                raw_cells = {h: (row[i] if i < len(row) else "") for i, h in enumerate(header) if h}
                fields: dict[str, str] = {}
                for i, slot in col_map.items():
                    if slot in ("unit", "qty"):
                        continue  # 下面用锚点重新定位，不信任表头绝对下标
                    if i < len(row) and row[i]:
                        val = row[i]
                        if slot in ("name", "spec"):
                            val = _strip_wrap_escape(val)
                        fields[slot] = val
                # 单位/数量：材质子列区间任一格被 Paddle 压缩掉都会让这两列整体
                # 左移（模块文档"跟报价侧同一个缺陷"）——按税率列的"NN%"形状做
                # 每行独立锚点重新定位，找不到锚点（没有税率列，或这一行没有
                # 唯一命中）就退回表头绝对下标，好过完全没有数据。
                row_rate_idx = _locate_tax_rate_idx(row) if rate_offsets else None
                if row_rate_idx is not None:
                    for slot, offset in rate_offsets.items():
                        i = row_rate_idx + offset
                        if 0 <= i < len(row) and row[i]:
                            fields[slot] = row[i]
                else:
                    for slot in ("unit", "qty"):
                        i = next((idx for idx, s in col_map.items() if s == slot), None)
                        if i is not None and i < len(row) and row[i]:
                            fields[slot] = row[i]
                if not fields.get("name") and not fields.get("seq"):
                    continue  # 关键字段都拿不到，大概率是脏行
                row_type = _classify_row_type(row, name_idx)
                collected.append({
                    **fields,
                    "_raw_cells": raw_cells,
                    "_page": page_1based,
                    "_row_type": row_type,
                })

    if not collected:
        return None

    row_keys = [(r.get("name", ""), r.get("spec", ""), r.get("unit", ""), r.get("qty", ""))
               for r in collected]
    copy_nos = detect_copies(row_keys)

    # 材质子列（"材质_阀体"这类）不进 `_CANONICAL_SLOTS`——它们原始表头文字
    # 各文档不一样（子列数量、名字都会变），照原样透传给 CSV，`build_tender_
    # fields` 按前缀从 `raw_cells` 扫，不依赖固定列位。
    material_headers = [h for h in extra_headers
                        if h not in map_columns([h], slots=TENDER_SLOTS)
                        and ("材质" in h or h.lower().startswith("material_"))]
    fieldnames = (["row_type"] + _CANONICAL_SLOTS + material_headers + ["copy_no", "page"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(fieldnames)
    for r, copy_no in zip(collected, copy_nos):
        row_out = [r["_row_type"]] + [r.get(s, "") for s in _CANONICAL_SLOTS]
        row_out += [r["_raw_cells"].get(h, "") for h in material_headers]
        row_out += [str(copy_no), str(r["_page"] or "")]
        writer.writerow(row_out)
    return buf.getvalue()


# ─── 生产入口 ─────────────────────────────────────────────────────────────
# 类型跟 paddle_vl.SubmitAndParse / paddle_doc_meta.TextCall 同一契约，这里不
# 重新 import Callable 只为标注两行类型别名，直接用注释说明。


def parse_tender_document_paddle(
    file_path: str, *, submit_and_parse, text_call=None, page_count: int,
    with_meta: bool = True, requirements=None, progress_cb=None,
):
    """生产入口：整份招标 PDF → Paddle 结构化 JSON → (采购清单 draft + 封面标量
    + 招标要求)。**只提交一次**——`submit_and_parse` 的返回同时喂给
    `build_tender_csv`（表格）和 `paddle_doc_meta`（每页文字），不重复调用
    Paddle（跟 `vl_tender.parse_tender_document` "渲染只做一次，两项共用同一批
    图像" 同一个理由，只是这里连 Paddle 调用本身也只有一次）。

    `text_call` 为 None（未配置文字抽取客户端）时标量/要求整体留空，不阻断
    清单——清单才是主线，跟 vision 路径 `extract_tender_meta`/
    `extract_tender_requirements` 失败不拖垮清单同一个约定。
    """
    from apps.api.core.domain_config import (
        PADDLE_EXPECTED_SECONDS_PER_PAGE,
        PADDLE_PROGRESS_ESTIMATE_CAP,
    )
    from apps.api.intelligence.paddle_doc_meta import (
        extract_meta_from_text,
        extract_requirements_from_text,
    )
    from apps.api.intelligence.vl_tender import (
        _META_KEYS,
        DEFAULT_TENDER_REQUIREMENTS,
        META_PAGES,
        TenderParseResult,
    )

    # 阶段命名/进度估算跟 paddle_vl.recognize_quote_paddle 同一套（design/27
    # §6），两处独立实现是因为分属不同模块、不同调用契约，逻辑本身不重复设计。
    def _notify(stage: str, pct: int, *, stage_current: int | None = None,
               stage_total: int | None = None) -> None:
        if progress_cb:
            progress_cb(stage, pct, stage_current=stage_current, stage_total=stage_total)

    expected_s = PADDLE_EXPECTED_SECONDS_PER_PAGE * page_count if page_count else None

    def _poll_progress(elapsed_s: float, poll_expected_s: float | None) -> None:
        if poll_expected_s:
            frac = min(elapsed_s / poll_expected_s, PADDLE_PROGRESS_ESTIMATE_CAP)
            pct = 20 + int(70 * frac)
        else:
            pct = 55
        _notify("识别内容", pct, stage_current=int(elapsed_s),
               stage_total=int(poll_expected_s) if poll_expected_s else None)

    _notify("识别内容", 20, stage_current=0,
           stage_total=int(expected_s) if expected_s else None)
    doc_json = submit_and_parse(file_path, page_count=page_count, progress_cb=_poll_progress)
    pages = doc_json.get("pages") or []
    page_text_by_num = {
        p.get("page_num"): (p.get("text") or "") for p in pages if isinstance(p.get("page_num"), int)
    }
    # Paddle 页码 0 起，跟本模块/CSV 其余地方的 1-based 约定对齐。
    all_texts = [page_text_by_num[n] for n in sorted(page_text_by_num)]
    meta_texts = all_texts[:META_PAGES]

    if text_call is None:
        meta = {k: "" for k in _META_KEYS}
        req_out: dict = {}
    else:
        _notify("提取信息", 92)
        meta = extract_meta_from_text(meta_texts, text_call) if with_meta else {k: "" for k in _META_KEYS}
        reqs = requirements if requirements is not None else DEFAULT_TENDER_REQUIREMENTS
        req_out = extract_requirements_from_text(all_texts, text_call, reqs) if reqs else {}

    _notify("整理完成", 97)
    csv_text = build_tender_csv(doc_json)
    if csv_text is None:
        csv_text = "row_type," + ",".join(_CANONICAL_SLOTS) + ",copy_no,page\n"
    processed_pages = list(range(1, page_count + 1))
    draft = build_tender_draft(csv_text, file_path=file_path, page_count=page_count,
                               processed_pages=processed_pages, parser_mode=PARSER_MODE)
    draft.meta["tender_meta"] = meta
    draft.meta["tender_requirements"] = req_out
    return TenderParseResult(draft=draft, meta=meta, requirements=req_out,
                             rotations={}, unresolved_pages=[])
