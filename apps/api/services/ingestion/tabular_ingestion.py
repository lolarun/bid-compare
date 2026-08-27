"""tabular_ingestion.py — Deterministic CSV / Excel quote extractor.

Entry point: extract_quote_tabular(file_path, ctx) -> dict

Returns the same result shape as pipeline.ExtractionPipeline._postprocess_quote so
that document_ingestion.run_job can write job.result and let the shared downstream
(batch-confirm → anchor-match → 90-row matrix) proceed unchanged.

Design constraints (from approved plan):
- One file = one supplier.  Multi-supplier column guard hard-rejects the file.
- bid_total MUST come from an independent totals row in the file, NEVER from
  summing line items (that would be a self-referential tautology).
- Column detection uses this module's own _TABULAR_COLUMN_PATTERNS instead of
  import_service._COLUMN_PATTERNS: the import_service pattern maps 价税合计 to
  the "price" (unit-price) slot which is wrong for report tables where 价税合计
  is almost always the grand-total row, not a per-item unit price.
- 0 items after parsing → ValueError (hard fail; prevents silent empty uploads).
- source_ref is preserved until batch-confirm only.  It is NOT persisted in the
  Quote model.  If future LLM Top-K judging needs row-level evidence, add a
  Quote.source_ref_json column at that point.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from apps.api.core.utils import parse_rate
from apps.api.intelligence.column_roles import propose_by_llm, verify_roles
from apps.api.intelligence.price_basis import derive_price_basis

log = logging.getLogger(__name__)

# ─── tabular-specific column patterns ────────────────────────────────────────
# Separate from import_service._COLUMN_PATTERNS:
# - "unit_price": only genuine per-item unit-price columns (NOT 价税合计)
# - "total_price": per-row extended amount (合价/金额/价税合计 when not a footer)
# - Longest/most-specific patterns are listed first; first match wins.
#
# Note: 含税单价 < 单价 priority ensures more-specific pattern wins when both columns
# exist (e.g. "含税单价" and "不含税单价" in the same file).

# Detection order is SIGNIFICANT: more-specific price roles must be detected
# before the generic unit_price, and each column is claimed at most once (see
# _detect_tabular_columns). This prevents "不含税单价" from being grabbed by
# unit_price (it contains the substring "含税单价"). Defense-in-depth: incl-tax
# patterns carry a (?<!不) negative lookbehind so they never match a 不含税 column
# even if it were somehow still a candidate.
_TABULAR_COLUMN_PATTERNS: dict[str, list[str]] = {
    "name":           [r"材料名称", r"设备名称", r"物料名称", r"名称", r"品名"],
    "spec":           [r"规格型号", r"规格", r"型号"],
    "brand":          [r"品牌", r"厂家", r"制造商"],
    "supplier":       [r"供应商", r"投标单位", r"报价单位", r"厂商名称"],
    "material_type":  [r"材质", r"牌号"],
    "unit":           [r"计量单位", r"单位"],
    "quantity":       [r"数量", r"工程量", r"数 量"],
    # ── 价格列：**先认税基，通用槽兜底** ────────────────────────────────────
    # 顺序是判据的一部分：带税基标签的列必须先被认走，剩下的才交给通用槽。
    # 通用 `unit_price`/`total_price` 的语义是「原文没区分含税/不含税的单一
    # 价格列」（绵存那种只有"单价/合价"两列的表），**不是**"任何单价列"。
    #
    # 2026-08-23 之前这里只有 `unit_price_excl_tax` 一个税基槽，`合计(不含税)`/
    # `税率`/`税额` 三列没有归宿被直接丢弃，而 `价税合计`/`合价(含税)` 被通用
    # `total_price` 认走。后果：泰科龙那份（只有 不含税单价 + 价税合计，没有含税
    # 单价列）拿 `unit_price_excl_tax=69.12` 对上 `total_price=78.1`，89 行全部
    # 判成 `tax_basis_suspect`，整份 submission 被 `systematic_vat_mismatch`
    # 挡在正式比价外——而 `draft_integrity._PRICE_PAIRS` 的注释早就写着这正是
    # "比错了尺子"。数据一直在文件里，只是没有字段接。
    "tax_rate":       [r"税率"],
    "tax_amount":     [r"税额", r"税金"],
    "total_price_excl_tax": [r"不含税合价", r"不含税合计", r"不含税.*合价", r"不含税.*合计",
                             r"合价.*不含税", r"合计.*不含税"],
    # 价税合计 = 逐行的含税合价列，不是表尾合计行（表尾靠 `_row_is_total()` 认）。
    "total_price_incl_tax": [r"价税合计", r"(?<!不)含税合价", r"(?<!不)含税合计",
                             r"(?<!不)含税.*合价", r"合价.*(?<!不)含税", r"合计.*(?<!不)含税"],
    "unit_price_excl_tax": [r"不含税单价", r"不含税.*单价", r"单价.*不含税", r"不含税价"],
    "unit_price_incl_tax": [r"(?<!不)含税单价", r"(?<!不)含税.*单价", r"单价.*(?<!不)含税",
                            r"(?<!不)含税价(?!合)"],
    # 通用槽最后认，只捡没被税基槽claim走的列。
    "total_price":    [r"合价", r"合计", r"金额", r"总价(?!.*合计)"],
    "unit_price":     [r"(?<!合)单价(?!.*合)"],
    "remark":         [r"备注", r"说明"],
}


def _detect_tabular_columns(columns: list[str]) -> dict[str, str | None]:
    """Detect column roles using _TABULAR_COLUMN_PATTERNS.

    Iterates roles in dict order (significant — see comment above); within a
    role, first matching unclaimed column wins. A column claimed by an earlier
    role (e.g. unit_price_excl_tax) is excluded from later roles (unit_price),
    so "不含税单价" can never be mis-assigned to unit_price.
    """
    result: dict[str, str | None] = {}
    claimed: set[str] = set()
    for field, patterns in _TABULAR_COLUMN_PATTERNS.items():
        result[field] = None
        for pattern in patterns:
            for col in columns:
                if col in claimed:
                    continue
                if re.search(pattern, str(col)):
                    result[field] = col
                    claimed.add(col)
                    break
            if result[field]:
                break
    return result


def _get_cell(row, col_map: dict, field: str) -> str | None:
    """Read a cell value from a pandas row via col_map; return stripped str or None."""
    import pandas as pd
    col = col_map.get(field)
    if not col:
        return None
    val = row.get(col)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s and s != "nan" else None


# ─── better number parser ─────────────────────────────────────────────────────

def _parse_number(s: str | None) -> float | None:
    """Parse a numeric string from Chinese supplier tables.

    Handles:
    - Thousand separators: "1,234.56" → 1234.56
    - Chinese full-width commas/periods: "１，２３４．５６"
    - Leading currency symbols: "¥1,234.56", "￥1,234.56"
    - Trailing units: "1,234.56元", "1000个"
    - Empty / NaN strings → None
    - Negative values preserved

    Does NOT handle European "1.234,56" (rare in Chinese supply chain).
    """
    if s is None or s == "":
        return None
    # Full-width normalisation
    s = s.translate(str.maketrans("０１２３４５６７８９．，￥", "0123456789.,¥"))
    # Strip currency symbols and whitespace
    s = s.strip().lstrip("¥$€£").strip()
    # Remove thousand-separator commas (e.g. "1,234.56")
    # Safe only when comma is followed by exactly 3 digits before next comma/dot/end
    s = re.sub(r",(\d{3})(?=[,\d.]|$)", r"\1", s)
    # Also handle plain comma separators without strict positional check
    s = s.replace(",", "")
    # Strip trailing non-numeric characters (units like 元/个/套)
    s = re.sub(r"[^\d.\-].*$", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        v = float(s)
        return v if v == v else None  # guard NaN
    except ValueError:
        return None


def _to_positive(s: str | None) -> float | None:
    """Parse number and return None if ≤ 0."""
    v = _parse_number(s)
    return v if v is not None and v > 0 else None


# ─── footer / totals markers ─────────────────────────────────────────────────
# Used to detect a "grand total" row that supplies the checksum-safe bid_total.
# Rows matching these markers are EXCLUDED from items.

_TAX_INCL_MARKERS = ("价税合计", "含税合价", "含税合计", "含税总价", "含税小计")
_TAX_EXCL_MARKERS = ("不含税合计", "不含税总价", "不含税小计")
_GENERIC_TOTAL_MARKERS = ("合计", "总计", "总价", "总报价", "小计")


def _detect_total_row_basis(cell_text: str) -> str | None:
    """Return bid_total_basis if cell_text looks like a totals-row marker, else None."""
    for m in _TAX_INCL_MARKERS:
        if m in cell_text:
            return "tax_included"
    for m in _TAX_EXCL_MARKERS:
        if m in cell_text:
            return "tax_excluded"
    for m in _GENERIC_TOTAL_MARKERS:
        if m in cell_text:
            return "unknown"
    return None


def _row_is_total(row_cells: list[str]) -> str | None:
    """If any cell in the row contains a totals marker, return the basis; else None."""
    for cell in row_cells:
        basis = _detect_total_row_basis(cell)
        if basis is not None:
            return basis
    return None


# ─── CSV loading with encoding fallback ──────────────────────────────────────

# 表头行往下最多找几行。真实语料里标题/抬头最多占两三行（项目名、编号、
# 日期各一行），给 8 行余量；再往下还找不到名称列，那就是这份表真的没有
# 名称列，该按原来的方式报错，不该无限找下去把某个数据行当成表头。
_HEADER_SCAN_ROWS = 8


def _find_header_row(raw) -> int:
    """在前几行里找出真正的表头行，返回它的行号（0 = 第一行，即旧行为）。

    **判据复用 `_detect_tabular_columns`**：哪一行能被识别出「名称」列，哪一行
    就是表头。不另写一套关键词——两套关键词迟早会漂移，而这里判错的后果是
    整份文件解析失败。

    2026-08-23 之前这里写死 `header=0`，凡是表头上面还有标题行的工作簿一律
    解析失败。真实语料里这是常态而不是例外：金桥采购清单第一行是「金桥地铁
    上盖J9A-03地块（浦发上城科创智谷）研发及商业项目阀门投标清单」，徐汇采购
    清单第一行是「上海市徐汇区华泾镇XHPO-0001单元D5B-1地块一期项目综合机电
    专业分包工程电缆报价清单」，两份都因此报「未识别到物料名称列。实际列名：
    ['<整行标题>', 'Unnamed: 1', ...]」。

    这个能力**本来就存在于别处**：`parse_tender_xlsx`（采购清单路径）和测试里的
    `read_reference` 都是扫描找表头的，只有生产报价路径没有。三处读表格、两处
    会找表头，是这个缺陷能活这么久的原因。
    """
    for i in range(min(_HEADER_SCAN_ROWS, len(raw))):
        cells = [str(c) for c in raw.iloc[i].tolist() if str(c) != "nan"]
        if cells and _detect_tabular_columns(cells).get("name"):
            return i
    return 0


def _load_dataframe(file_path: str):
    """Load an Excel or CSV file into a pandas DataFrame (dtype=str).

    表头行由 `_find_header_row` 定位，不假设它在第一行。
    CSV: tries utf-8-sig first, falls back to gbk (common for Chinese suppliers).
    Raises ValueError with a user-friendly message on parse failure.
    """
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pandas is required for tabular ingestion") from e

    ext = Path(file_path).suffix.lower()
    if ext in {".xlsx", ".xls"}:
        try:
            # 先按"无表头"读一遍定位表头行，再按该行重读。多读一次的代价换
            # 一个不会因为一行标题就整份失败的解析器。
            raw = pd.read_excel(file_path, header=None, dtype=str)
            return pd.read_excel(file_path, header=_find_header_row(raw), dtype=str)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Excel 文件解析失败: {e}") from e
    else:
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                raw = pd.read_csv(file_path, header=None, dtype=str, encoding=enc)
                return pd.read_csv(file_path, header=_find_header_row(raw),
                                   dtype=str, encoding=enc)
            except UnicodeDecodeError:
                continue
            except Exception as e:
                raise ValueError(f"CSV 文件解析失败: {e}") from e
        raise ValueError("CSV 文件编码无法识别，请另存为 UTF-8 格式后重试")


def _sample_rows(df, limit: int | None = None) -> list[list[str]]:
    """DataFrame → `list[list[str]]`，NaN 变空串。验证器和模型提议都吃这个形状。"""
    sub = df if limit is None else df.head(limit)
    return [["" if v is None or (isinstance(v, float) and v != v) else str(v).strip()
             for v in row] for row in sub.values.tolist()]


def resolve_columns(df) -> tuple[dict[str, str | None], str, list[str]]:
    """列 → 角色：**词表 → 验证 → 验不过才问模型 → 再验一次**（design/40 §5）。

    返回 `(col_map, 来源, 未通过的理由)`。来源三态，会写进 `_doc_meta` 供审计：
      `keyword`             词表判定且通过验证——**已知形状走这条，零模型调用**；
      `llm`                 词表没通过、模型提议通过了验证；
      `keyword_unverified`  两条路都没通过验证，退回词表结果并把理由带出去。

    最后那一态是有意保留的：**验不过不等于解析不出东西**。词表可能只是漏认了
    某个可选列（品牌、备注），据此拒绝整份上传的代价远大于带着诊断继续。真正
    致命的缺失（没有名称列）由调用方单独拦，那条判据没变。

    模型只回答"哪一列是什么"，一张表一次，且产出必须过同一道确定性验证
    （`column_roles.verify_roles`）。行级的事——哪一行对哪一条锚点——不走这里，
    数量序列已经确定性可解（design/39）。
    """
    cols = [str(c) for c in df.columns.tolist()]
    rows = _sample_rows(df)
    keyword = _detect_tabular_columns(cols)
    kw_roles = {k: cols.index(v) for k, v in keyword.items() if v in cols}
    verdict = verify_roles(kw_roles, rows)
    if verdict.ok:
        return keyword, "keyword", []

    log.info("列判据未通过词表映射，转模型提议：%s", verdict.reasons)
    # 延迟导入：`paddle_doc_meta` 会把 dashscope 客户端拉进来，而本模块的主路径
    # （词表通过）根本不需要它。没配 key 时它返回 None，按"没有兜底"处理，不抛。
    from apps.api.intelligence.paddle_doc_meta import get_text_client_call
    call = get_text_client_call()
    if call is not None:
        proposed = propose_by_llm(cols, rows, call)
        if proposed:
            merged = _merge_proposal(kw_roles, proposed, missing_only=_only_missing(verdict))
            v2 = verify_roles(merged, rows)
            if v2.ok:
                log.info("列判据改用模型提议：%s", {k: cols[i] for k, i in merged.items()})
                return ({k: cols[i] for k, i in merged.items()}, "llm", [])
            log.info("模型提议同样未通过验证，退回词表：%s", v2.reasons)
    return keyword, "keyword_unverified", verdict.reasons


def _only_missing(verdict) -> bool:
    """词表失败**只是因为缺角色**（而不是因为证据打架）？

    两种失败该有两种处置，这是收窄"数量↔单价对调"风险的地方
    （见 `column_roles.verify_roles` 文档：算术对这对互换是盲的）：

    - **只是缺角色** → 词表对它认出来的那些列是有依据的（列名就那么写着），
      模型只准填空格，**不准改已认出的角色**。数量列如果词表凭列名认出来了，
      模型就动不了它；词表认不出的表，本来也没有第二个意见可以对照。
    - **证据打架**（某列不是数、名称列全是数字、算术不闭合）→ 词表的答案已经
      被数据证伪，整份换成模型的，再过一次验证。
    """
    return all("没有认出" in r for r in verdict.reasons)


def _merge_proposal(kw_roles: dict[str, int], proposed: dict[str, int],
                    *, missing_only: bool) -> dict[str, int]:
    if not missing_only:
        return dict(proposed)
    merged = dict(kw_roles)
    claimed = set(kw_roles.values())
    for role, idx in proposed.items():
        if role in merged or idx in claimed:
            continue
        merged[role] = idx
        claimed.add(idx)
    return merged


# ─── main extractor ───────────────────────────────────────────────────────────

def extract_quote_tabular(file_path: str, ctx: dict) -> dict:
    """Extract quote items from an Excel / CSV file.

    Args:
        file_path: absolute path to the file on disk.
        ctx: ExtractionJob.context dict; may contain supplier_name, project_id, etc.

    Returns:
        dict with keys: supplier_name, quote_date, items, context, _doc_meta
        Shape is identical to pipeline._postprocess_quote output so that
        document_ingestion.run_job can write it to job.result unchanged.

    Raises:
        ValueError: for multi-supplier files, unreadable files, missing name
                    column, or zero valid items after parsing.  Zero items is
                    treated as a hard failure to prevent silent empty uploads.
    """
    import pandas as pd
    from apps.api.intelligence.quote_fact import quote_fact_from_row, apply_arithmetic_validation

    # ── 1. Load ───────────────────────────────────────────────────────────────
    df = _load_dataframe(file_path)
    if df.empty:
        raise ValueError("文件为空，未找到任何数据行")

    # ── 2. Column detection ───────────────────────────────────────────────────
    col_map, col_source, col_reasons = resolve_columns(df)

    if not col_map.get("name"):
        raise ValueError(
            f"未识别到物料名称列（尝试列名：名称/品名/材料名称/设备名称）。"
            f"实际列名：{df.columns.tolist()}"
            + (f"。列判据不通过：{'；'.join(col_reasons)}" if col_reasons else "")
        )

    # ── 3. Multi-supplier guard ───────────────────────────────────────────────
    supplier_col = col_map.get("supplier")
    if supplier_col:
        unique_suppliers = (
            df[supplier_col]
            .dropna()
            .astype(str)
            .str.strip()
            .replace("nan", pd.NA)
            .dropna()
            .unique()
        )
        unique_suppliers = [s for s in unique_suppliers if s]
        if len(unique_suppliers) > 1:
            raise ValueError(
                f"检测到多供应商列 ({len(unique_suppliers)} 家)，"
                "请按供应商拆分后单独上传"
            )

    # ── 4. Supplier name resolution ───────────────────────────────────────────
    # Priority: (a) explicit ctx key → (b) first non-empty supplier column cell → (c) ""
    supplier_name: str = (ctx.get("supplier_name") or "").strip()
    if not supplier_name and supplier_col:
        first_sup = _get_cell(df.iloc[0], col_map, "supplier")
        if first_sup:
            supplier_name = first_sup

    # ── 5. Row iteration ──────────────────────────────────────────────────────
    items: list[dict] = []
    bid_total: float | None = None
    bid_total_basis: str = "unknown"
    found_total_row: bool = False

    for idx, row in df.iterrows():
        # Stringify all non-empty cells for totals-row marker scan
        row_cells = [
            str(v).strip()
            for v in row.values
            if v is not None and str(v).strip() not in ("", "nan")
        ]

        # ── totals-row check ──────────────────────────────────────────────────
        basis = _row_is_total(row_cells)
        if basis is not None:
            # Try to extract bid_total from the total_price column (preferred)
            # or fall back to the unit_price column (some files put grand total there).
            if not found_total_row:
                raw_total = _get_cell(row, col_map, "total_price") or _get_cell(row, col_map, "unit_price")
                if raw_total:
                    v = _parse_number(raw_total)
                    if v is not None and v > 0:
                        bid_total = v
                        bid_total_basis = basis
                        found_total_row = True
                        log.debug(
                            "tabular_ingestion: totals row idx=%s  bid_total=%s basis=%s",
                            idx, bid_total, bid_total_basis,
                        )
            # Exclude from items regardless
            continue

        # ── name check ────────────────────────────────────────────────────────
        raw_name = _get_cell(row, col_map, "name")
        if not raw_name:
            continue

        # ── numeric fields ────────────────────────────────────────────────────
        qty        = _to_positive(_get_cell(row, col_map, "quantity"))
        unit_price = _to_positive(_get_cell(row, col_map, "unit_price"))
        unit_price_incl = _to_positive(_get_cell(row, col_map, "unit_price_incl_tax"))
        unit_price_excl = _to_positive(_get_cell(row, col_map, "unit_price_excl_tax"))

        # per-row extended amount (合价): prefer explicit column, derive if absent
        raw_total_price = _get_cell(row, col_map, "total_price")
        total_price = _to_positive(raw_total_price)
        total_price_incl = _to_positive(_get_cell(row, col_map, "total_price_incl_tax"))
        total_price_excl = _to_positive(_get_cell(row, col_map, "total_price_excl_tax"))
        # 税率既可能印成 0.13 也可能印成 13%/13——归一化交给共享的 `parse_rate`，
        # 不在这里再写一份（`paddle_vl._parse_rate` 曾是第二份，已收拢）。
        tax_rate = parse_rate(_get_cell(row, col_map, "tax_rate"))
        tax_amount = _to_positive(_get_cell(row, col_map, "tax_amount"))
        # If no total column at all, QuoteFact.__post_init__ records total_source=missing

        spec   = _get_cell(row, col_map, "spec")   or ""
        brand  = _get_cell(row, col_map, "brand")  or ""
        unit   = _get_cell(row, col_map, "unit")   or ""
        material_type = _get_cell(row, col_map, "material_type") or ""
        remark = _get_cell(row, col_map, "remark") or ""

        # ── construct QuoteFact ───────────────────────────────────────────────
        fact = quote_fact_from_row(
            material=raw_name.strip(),
            spec=spec.strip(),
            brand=brand.strip(),
            unit=unit.strip(),
            qty=qty,
            unit_price=unit_price,
            unit_price_incl_tax=unit_price_incl,
            unit_price_excl_tax=unit_price_excl,
            total_price=total_price,
            total_price_incl_tax=total_price_incl,
            total_price_excl_tax=total_price_excl,
            tax_rate=tax_rate,
            tax_amount=tax_amount,
            material_type=material_type.strip(),
            remark=remark.strip(),
            source_ref={"sheet": _sheet_name(file_path), "row": int(idx) + 2},
        )
        # 价格口径与比价有效价（design/29 §11.1）。**复用 PDF 路径同一个
        # `derive_price_basis`，不另写一套口径判定**——两套迟早会漂，而这里判的
        # 是"这一行的钱该按哪个税基读"，两条上传路径必须给出同一个答案。
        #
        # 2026-08-25 补：此前 Excel 这条路**完全没有产出** `effective_total_price`
        # （全文件零处），只有 PDF 路径（`pipeline.py:506`）有。前端
        # `bidStatsFor` 读 `effective_total_price ?? total_price`，于是表头区分
        # 含税/不含税的报价单（泰科龙、凯硕：值落在 `total_price_excl_tax`、
        # 通用 `total_price` 本来就是空的）退回读空槽位，**卡片上"明细合计"
        # 显示成 ¥0**。绵存看着正常纯属巧合——它表头是通用的"单价/合价"，
        # 值恰好落在 `total_price`。修的是缺失，不是改任何原值。
        item = fact.to_item_dict()
        item.update(derive_price_basis(item))
        items.append(item)

    # ── 6. Hard fail on 0 items ───────────────────────────────────────────────
    # Prevents silent empty uploads that would pass batch-confirm but produce
    # zero rows in the bid matrix with no visible error.
    if not items:
        raise ValueError(
            "未解析到有效报价行（名称列已识别但所有行为空或被过滤）。"
            "请检查文件格式是否与列检测规则匹配。"
        )

    # ── 7. Arithmetic validation (same rule as PDF path) ─────────────────────
    apply_arithmetic_validation(items)

    # ── 8. Assemble result (same shape as _postprocess_quote) ─────────────────
    doc_meta: dict[str, Any] = {
        "bid_total": bid_total,
        "bid_total_basis": bid_total_basis,
        "supplier_name": supplier_name,
        # 列映射是**怎么来的**必须随行走：审计时要能分清"这份的单价列是词表认的"
        # 和"是模型认的"，后者出问题的排查路径完全不同。`keyword_unverified`
        # 还带着未通过的理由，不留一个只写着"成功"的黑箱。
        "column_source": col_source,
        "column_warnings": col_reasons,
    }

    log.info(
        "tabular_ingestion: %s → %d items, bid_total=%s (%s)",
        Path(file_path).name, len(items), bid_total, bid_total_basis,
    )

    # 品类：与 PDF 侧（`pipeline._postprocess_quote`）同一个函数、同一套阈值。
    # 两条路产出同样形状的结果，前端不必分辨这份报价当初是怎么进来的。
    from apps.api.services.ingestion.category_classify import detect_category_from_items

    return {
        "supplier_name": supplier_name,
        "quote_date": "",
        "items": items,
        "detected_category": detect_category_from_items(items),
        "context": ctx,
        "_doc_meta": doc_meta,
    }


# ─── private helpers ──────────────────────────────────────────────────────────

def _sheet_name(file_path: str) -> str:
    """Return first sheet name for xlsx, or filename stem for csv."""
    ext = Path(file_path).suffix.lower()
    if ext in {".xlsx", ".xls"}:
        try:
            import pandas as pd
            xl = pd.ExcelFile(file_path)
            return xl.sheet_names[0] if xl.sheet_names else "Sheet1"
        except Exception:
            pass
    return Path(file_path).stem
