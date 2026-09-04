"""tender_text_layer.py — docs/design/25 轨A：招标采购清单文字层直抽。

只处理**有可用文字层的原生 PDF**（born-digital，不是扫描件）。检测与抽取都不碰
`vl_tender.py`/`vl_quote.py` 的 VL-direct 主链路——`.claude/rules/recognition.md`
禁止的是"部分表格走确定性结构、复杂表头走 LLM fallback"这种**同一份文档内部**
按表格复杂度分流的双路径（那是已删除的 legacy TableGrid 链路想解决的问题，且
文档内每张表都要经过一次视觉判断"这张复杂不复杂"）。这里是**文档级**的前置判断：
判断依据是文件本身有没有文字层（PDF 结构事实，渲染前一次性确定，不逐表判断），
判断成立时**完全不调用视觉模型**，不是"部分用、部分不用"。判断不成立（无文字层，
或抽出来的表结构不可信）时整份回落现有 VL-direct 路径，不做任何形式的混合。

采购清单表（本模块唯一负责的部分）走确定性抽取：pdfplumber 读文字层 → 表格结构
（含两级表头拍平）→ 拼一份 CSV → 喂给 `vl_quote.build_draft` 复用的同一套结构门
（列错位、截断、序号连续性、行数台账）——`parser_mode="text_layer"` 标注来源，
不冒充 vl_direct（评审 N1 的教训：标签必须诚实反映真实来源）。

封面标量（项目名称/编号/招标单位/日期/截止时间）仍走 VL：那是首页落款里的自由文本，
不同文档措辞差异大，正则提取脆弱且样本特定；两页的小调用相对于整份清单的渲染+
识别成本很小，不是这一轮要省的大头。

品牌要求表（业主品牌要求 + 各投标单位参与品牌）**也**是清单之外的一张独立结构化
表格，同样走确定性抽取，不调用视觉模型——理由与采购清单表一致。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅供注解；运行时由调用方传入已构造好的 draft
    from apps.api.intelligence.extraction_draft import ExtractionDraft

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ─── §1 检测：是否有可用文字层 ────────────────────────────────────────────────

TEXT_LAYER_SAMPLE_PAGES = 5     # 抽样前几页做检测，不必读完整份文档
TEXT_LAYER_MIN_CHARS = 200      # 阈值：假阴性（判成无文字层，落回 VL）是安全的，
                                 # 只需要防假阳性（把带几个杂散字符的扫描件误判为
                                 # 有文字层）——200 字符是一句话都不止，扫描件的
                                 # 文字层残留噪声不太可能凑够这个量。


def has_usable_text_layer(file_path: str, *,
                          sample_pages: int = TEXT_LAYER_SAMPLE_PAGES,
                          min_chars: int = TEXT_LAYER_MIN_CHARS) -> bool:
    """PDF 是否有可用文字层。不渲染任何页面——纯文字层读取，比 VL 路径的探测
    便宜得多。判定失败（异常/无文字层）一律返回 False，交给调用方回落 VL。"""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(file_path)
        try:
            n = min(len(doc), sample_pages)
            total = 0
            for i in range(n):
                total += len(doc[i].get_textpage().get_text_range().strip())
            return total >= min_chars
        finally:
            doc.close()
    except Exception:                                              # noqa: BLE001
        log.warning("has_usable_text_layer 检测失败，按无文字层处理", exc_info=True)
        return False


# ─── §2 表格类型识别（关键词匹配，不认页码） ──────────────────────────────────
#
# 招标文件的采购清单页/品牌要求页位置不固定（跟 vl_tender.py 的"送整份"是同一个
# 理由）——按关键词识别表头，不按固定页号，换一份文档、清单挪了几页，这里不用改。

_ANCHOR_HEADER_HINTS = ("序号", "数量")           # 采购清单表：必须同时出现
_BRAND_HEADER_HINTS = ("业主", "品牌", "投标单位")  # 品牌要求表：至少出现两个


def _flat(cell) -> str:
    return re.sub(r"\s+", "", str(cell or ""))


def _looks_like_anchor_header(row: list) -> bool:
    joined = "".join(_flat(c) for c in row)
    return all(h in joined for h in _ANCHOR_HEADER_HINTS)


def _looks_like_brand_header(row: list) -> bool:
    joined = "".join(_flat(c) for c in row)
    return sum(1 for h in _BRAND_HEADER_HINTS if h in joined) >= 2


# ─── §3 采购清单表：两级表头拍平 + CSV 拼装 ───────────────────────────────────

def _flatten_anchor_header(header_row: list, sub_row: list | None) -> list[str]:
    """两级表头拍平成「父列_子列」。子列行的 None 表示该列延续上一个非空父列
    （pdfplumber 对合并单元格的表示方式）——「材质」跨阀体/阀芯/阀板/阀杆/密封圈
    五个子列时，父列行在这五个位置都是 None，子列行才是真正的列名。

    没有子列行（单级表头）时原样返回，去空白。这跟 vl_tender.py 提示词规则1
    要求模型做的事一样，只是这里是从文字层的表格结构直接推，不需要问模型。
    """
    parents = [_flat(c) for c in header_row]
    if sub_row is None:
        return parents
    out = []
    last_parent = ""
    for i, (p, s) in enumerate(zip(parents, (_flat(c) for c in sub_row))):
        if p:
            last_parent = p
        if s:
            out.append(f"{last_parent}_{s}" if last_parent else s)
        else:
            out.append(p or last_parent)
    return out


def _row_type_for(seq_val: str, first_nonempty: str) -> str:
    if seq_val.strip().isdigit():
        return "detail"
    if any(k in first_nonempty for k in ("小计", "合价", "合计", "总计")):
        return "total" if "总" in first_nonempty or "合价" in first_nonempty else "subtotal"
    return "detail"


def _table_to_anchor_csv_rows(
    table: list[list], page_num: int, carried_header: list[str] | None = None,
) -> tuple[list[str] | None, list[list[str]]]:
    """一页的 pdfplumber 表格 → (拍平表头, CSV 行数据)。不是清单表且没有可沿用
    的表头返回 (None, [])。

    结构假设（跟 vl_tender.py PROMPT_TENDER_CSV 的规则同构，不是这份样本专属）：
    第一行可能是标题行（单列跨全宽，不含表头关键词）；紧接着 1-2 行表头；
    此后是数据行，序号列非空即视为有效明细行。

    `carried_header`：清单跨页时，续页的表格**通常没有自己的表头行**——直接从
    序号延续的数据行开始（实测：金桥招标 89 行分布在 5 页，只有第一页有表头）。
    这不是本模块凭空假设，是这份文档的真实结构；沿用上一页表头是唯一诚实的
    处理方式——不沿用会把续页整段丢掉（PaddleOCR-VL 候选评测那轮踩过同一个坑，
    见 docs/design/26 §2.3 相关脚本）。判据：本页首行第一格是数字（延续序号），
    且列数与沿用表头一致；两者有一个不满足就不当续页处理，避免把无关表格
    误当续页吞进来。
    """
    if not table:
        return None, []
    header_idx = None
    for i, row in enumerate(table[:4]):
        if _looks_like_anchor_header(row):
            header_idx = i
            break

    if header_idx is None:
        if carried_header is None:
            return None, []
        first_row = table[0]
        first_cell = _flat_keep_space(first_row[0]) if first_row else ""
        if not first_cell.isdigit() or len(first_row) != len(carried_header):
            return None, []
        flat_header = carried_header
        data_start = 0
    else:
        sub_row = None
        data_start = header_idx + 1
        if data_start < len(table):
            nxt = table[data_start]
            # 子列行的判据：不含"序号"（那是它自己不是另一级表头），且非空单元格数
            # 明显少于父列（子列只填在父列被合并的那几个位置）。
            nxt_nonempty = sum(1 for c in nxt if _flat(c))
            header_nonempty = sum(1 for c in table[header_idx] if _flat(c))
            if "序号" not in "".join(_flat(c) for c in nxt) and 0 < nxt_nonempty < header_nonempty:
                sub_row = nxt
                data_start += 1
        flat_header = _flatten_anchor_header(table[header_idx], sub_row)
        table = table[data_start:]
        data_start = 0

    out_rows: list[list[str]] = []
    for row in table[data_start:]:
        cells = [(_flat_keep_space(c)) for c in row]
        if not any(cells):
            continue
        first_nonempty = next((c for c in cells if c), "")
        seq_idx = next((i for i, h in enumerate(flat_header) if h == "序号"), 0)
        seq_val = cells[seq_idx] if seq_idx < len(cells) else ""
        rtype = _row_type_for(seq_val, first_nonempty)
        out_rows.append([rtype] + cells + [str(page_num)])
    return flat_header, out_rows


def _flat_keep_space(cell) -> str:
    """跟 _flat 不同：只去换行不去空格——数值/文字内容本身要保留，只有表头
    关键词匹配才需要整体去空白。"""
    return re.sub(r"[\r\n]+", "", str(cell or "")).strip()


def build_anchor_csv(pdf, page_count: int) -> tuple[str, list[int]] | None:
    """扫描全文档，拼出一份 vl_quote.parse_csv 认得的 CSV。找不到清单表返回 None
    （调用方据此回落 VL，不是本模块的职责边界内能处理的情形）。"""
    import csv
    import io

    header: list[str] | None = None
    last_page: int | None = None
    all_rows: list[list[str]] = []
    processed_pages: list[int] = []
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        for table in page.extract_tables():
            # 续页表头沿用只在页码连续时生效——隔了一页就不再是"清单没写完"，
            # 是别的表格（品牌要求/规格参考之类）恰好列数对上，不该被当续页吞并。
            carried = header if (last_page is not None and page_num == last_page + 1) else None
            flat_header, rows = _table_to_anchor_csv_rows(table, page_num, carried_header=carried)
            if flat_header is None:
                continue
            if header is None:
                header = flat_header
            last_page = page_num
            processed_pages.append(page_num)
            all_rows.extend(rows)

    if header is None or not all_rows:
        return None

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row_type", *header, "page"])
    w.writerows(all_rows)
    return buf.getvalue(), processed_pages


# ─── §4 品牌要求表：业主品牌要求 + 各投标单位参与品牌 ──────────────────────────

_BRAND_SPLIT_RE = re.compile(r"[、，,;；\n]+")
_BRAND_EN_CN_RE = re.compile(r"^([A-Za-z0-9\-\.\s]+)(.*)$")


def _split_brand_token(token: str) -> dict:
    """"KITZ开滋" → {brand_en: "KITZ", brand_cn: "开滋"}；纯中文/纯英文时另一列
    留空——不强行拆分不存在的部分（跟 vl_tender.py 的"没有就留空"是同一个原则）。
    """
    token = token.strip()
    if not token:
        return {}
    m = _BRAND_EN_CN_RE.match(token)
    if m and m.group(1).strip() and m.group(2).strip():
        return {"brand_en": m.group(1).strip(), "brand_cn": m.group(2).strip()}
    if re.fullmatch(r"[A-Za-z0-9\-\.\s]+", token):
        return {"brand_en": token, "brand_cn": ""}
    return {"brand_en": "", "brand_cn": token}


def build_brand_requirements(pdf) -> tuple[list[dict], list[dict], int | None]:
    """→ (brand_requirement, supplier_brands, 表格所在页 1-based)。

    找不到品牌要求表时返回 ([], [], None)——跟 extract_tender_requirements 失败时
    "留空不报错"是同一个约定（这项要求本来就不是每份招标文件都有）。
    """
    for i, page in enumerate(pdf.pages):
        for table in page.extract_tables():
            if not table:
                continue
            header_idx = next(
                (j for j, row in enumerate(table[:3]) if _looks_like_brand_header(row)), None)
            if header_idx is None:
                continue
            header = [_flat(c) for c in table[header_idx]]
            col = {name: idx for idx, name in enumerate(header)}
            brand_col = next((v for k, v in col.items() if "品牌要求" in k or ("业主" in k and "品牌" in k)), None)
            supplier_col = next((v for k, v in col.items() if "邀请投标" in k or "投标单位" in k and "参与" not in k), None)
            supplier_brand_col = next((v for k, v in col.items() if "参与品" in k), None)

            brand_requirement: list[dict] = []
            seen_brands: set[str] = set()
            supplier_brands: list[dict] = []
            for row in table[header_idx + 1:]:
                cells = [_flat_keep_space(c) for c in row]
                if not any(cells):
                    continue
                if brand_col is not None and brand_col < len(cells) and cells[brand_col]:
                    for token in _BRAND_SPLIT_RE.split(cells[brand_col]):
                        parsed = _split_brand_token(token)
                        if not parsed:
                            continue
                        key = (parsed.get("brand_en", ""), parsed.get("brand_cn", ""))
                        if key not in seen_brands:
                            seen_brands.add(key)
                            brand_requirement.append(parsed)
                if (supplier_col is not None and supplier_col < len(cells) and cells[supplier_col]):
                    supplier_brands.append({
                        "supplier_name": cells[supplier_col],
                        "brand": cells[supplier_brand_col] if (
                            supplier_brand_col is not None and supplier_brand_col < len(cells)) else "",
                    })
            if brand_requirement or supplier_brands:
                return brand_requirement, supplier_brands, i + 1
    return [], [], None


# ─── §5 编排：文字层直抽的完整招标解析 ─────────────────────────────────────────

@dataclass
class TextLayerTenderResult:
    """跟 vl_tender.TenderParseResult 的 draft/meta/requirements/rotations/
    unresolved_pages 字段一一对应——extract_bidlist 消费两者不需要区分来源。"""
    draft: ExtractionDraft
    meta: dict
    requirements: dict
    rotations: dict
    unresolved_pages: list


def parse_tender_document_text_layer(
    file_path: str, *, vl_call, progress_cb=None,
) -> TextLayerTenderResult | None:
    """文字层直抽的完整入口。抽取不可信时返回 None——调用方（tender_pdf.py）
    据此整份回落 parse_tender_document（VL-direct），不做部分结果的静默拼接。

    封面标量仍走 vl_call（§标头说明为什么）：只渲染 META_PAGES 页，
    不做方向预检（原生 PDF 天然不存在"扫描件歪了"的问题，见 §标头）。
    """
    import pdfplumber

    from apps.api.intelligence.document_loader import DocumentLoader
    from apps.api.intelligence.extraction_draft import ExtractionDraft  # noqa: F401
    from apps.api.intelligence.vl_tender import (
        _META_KEYS,
        META_PAGES,
        build_tender_draft,
        extract_tender_meta,
    )

    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    _notify("检测文字层", 10)
    page_count = DocumentLoader.get_page_count(file_path)

    with pdfplumber.open(file_path) as pdf:
        _notify("解析采购清单（文字层）", 30)
        anchor_result = build_anchor_csv(pdf, page_count)
        if anchor_result is None:
            log.info("tender_text_layer: 未找到可信的采购清单表，回落 VL-direct")
            return None
        csv_text, processed_pages = anchor_result

        _notify("解析品牌要求（文字层）", 50)
        brand_requirement, supplier_brands, brand_page = build_brand_requirements(pdf)

    _notify("读取封面信息", 70)
    meta_pages = [p for p in range(1, META_PAGES + 1) if p <= page_count]
    images = DocumentLoader.render_pages(file_path, meta_pages) if meta_pages else {}
    meta = (extract_tender_meta([images[p] for p in meta_pages if p in images], vl_call)
            if images else {k: "" for k in _META_KEYS})

    _notify("整理结果", 90)
    draft = build_tender_draft(
        csv_text, file_path=file_path, page_count=page_count,
        processed_pages=sorted(set(processed_pages)),
        rotations={}, unresolved_pages=[],
        parser_mode="text_layer",
    )
    if not draft.rows:
        log.info("tender_text_layer: CSV 结构门判空，回落 VL-direct")
        return None

    requirements = {
        "brand_requirement": brand_requirement,
        "supplier_brands": supplier_brands,
        "material_class": "",
    }
    draft.meta["tender_meta"] = meta
    draft.meta["tender_requirements"] = requirements
    draft.meta["brand_page"] = brand_page

    return TextLayerTenderResult(
        draft=draft, meta=meta, requirements=requirements,
        rotations={}, unresolved_pages=[],
    )
