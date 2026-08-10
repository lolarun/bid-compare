"""export_recognition_facts.py — 系统识别事实表三路对照导出器

三路来源对照:
  1. table_parser  — OCR HTML → table_parser 直接解析（原始，可能列错位/漏行）
  2. two_stage     — OCR HTML → LLM 两阶段提取（行数更完整，无价格列）
  3. db            — 数据库入库 quote 行（已清洗，可能含错位数据）

目的：审计各来源差异，不做业务结论（在报价明细解析可信前不判断缺报）。

用法:
    python scripts/export_recognition_facts.py \\
        --tender "docs/test/招标清单.xlsx" \\
        --quote "上海绵存:docs/test/上海绵存投标文件.pdf" \\
        --quote "凯硕新正:docs/test/凯硕新正投标文件.pdf" \\
        --quote "泰科龙:docs/test/泰科龙投标文件.pdf" \\
        --project-id 62 \\
        --output outputs/recognition_facts/

输出:
    tender_anchors.csv
    <供应商>_combined_audit.csv   (三路合并，含 source_type + flags 列)
    compare_summary.csv           (逐供应商三路行数/总额/差异对照)
    README.md
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from apps.api.services.tender_list import parse_tender_xlsx
from apps.api.intelligence.table_parser import html_to_table_grids

_OCR_CACHE_DIR = REPO_ROOT / "data" / "ocr_test"
_TWO_STAGE_DIR = REPO_ROOT / "data" / "two_stage"

# ── helpers ───────────────────────────────────────────────────────────────────

_UNIT_WHITELIST_RE = re.compile(
    r"^(个|只|台|套|件|组|根|米|m|m²|m³|kg|t|吨|条|块|片|副|对|批|项|所|樘|扇|"
    r"付|张|卷|盘|桶|箱|包|袋|瓶|支|管|节|段|孔|口|处|座|层|跨|"
    r"百米|千米|万米|万个|百个|千个)$",
    re.IGNORECASE,
)

_TOTAL_KEYWORDS_RE = re.compile(
    r"合计|总计|总价|投标总价|价税合计|含税总计|详见投标|详见清单"
)

_NUMBER_RE = re.compile(r"^[\d,，.\-\s]+$")


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[,，\s￥¥]", "", str(s))
    try:
        v = float(cleaned)
        return v if v != 0 else None
    except ValueError:
        return None


def _is_numeric_str(s: str) -> bool:
    return bool(s and _NUMBER_RE.match(s.strip()))


def _find_ocr_cache(pdf_path: Path) -> Path | None:
    c = _OCR_CACHE_DIR / f"{pdf_path.stem}__ocr.txt"
    return c if c.exists() else None


def _find_two_stage_cache(pdf_path: Path) -> Path | None:
    c = _TWO_STAGE_DIR / f"{pdf_path.stem}__two_stage.csv"
    return c if c.exists() else None


# ── OCR cache text parser ─────────────────────────────────────────────────────

def _parse_ocr_cache_txt(txt_path: Path) -> list[dict]:
    text = txt_path.read_text(encoding="utf-8", errors="replace")
    chunks = re.split(r"={20,}", text)
    pages = []
    for chunk in chunks:
        pm = re.search(r"Page\s+(\d+)", chunk)
        if not pm:
            continue
        hm = re.search(r"```html(.*?)```", chunk, re.S)
        html = hm.group(1).strip() if hm else chunk
        pages.append({"page": int(pm.group(1)), "html": html})
    return pages


# ── Source 1: table_parser rows ───────────────────────────────────────────────

def _slot(grid, row, slot: str) -> str:
    header = next((h for h, s in (grid.col_map or {}).items() if s == slot), None)
    return (row.cells.get(header) or "").strip() if header else ""


def _raw_cells(row) -> str:
    return " | ".join(f"{k}={v}" for k, v in row.cells.items() if v and str(v).strip())


def _table_parser_flags(row: dict) -> list[str]:
    flags = []
    unit = row.get("unit", "")
    qty = row.get("qty", "")
    up = row.get("unit_price", "")
    name = row.get("name", "")
    rt = row.get("row_type", "")

    # column misalignment: unit field contains non-unit text
    if unit and not _UNIT_WHITELIST_RE.match(unit) and len(unit) > 4:
        flags.append(f"col_misalign:unit='{unit}'")

    # qty non-numeric
    if qty and not _is_numeric_str(qty):
        flags.append(f"col_misalign:qty='{qty}'")

    # unit_price non-numeric
    if up and not _is_numeric_str(up):
        flags.append(f"col_misalign:unit_price='{up}'")

    # grand_total keywords in a quote_line row
    if rt == "quote_line" and _TOTAL_KEYWORDS_RE.search(name):
        flags.append("grand_total_as_quote_line")

    # single-row with huge total_price and no spec (likely an aggregate line)
    tp = _to_float(row.get("total_price", ""))
    if rt == "quote_line" and tp and tp > 500_000 and not row.get("spec"):
        flags.append(f"suspect_aggregate:total={tp:,.0f}")

    return flags


def _load_table_parser_rows(pdf_path: Path) -> list[dict]:
    ocr_cache = _find_ocr_cache(pdf_path)
    if not ocr_cache:
        return []
    pages = _parse_ocr_cache_txt(ocr_cache)
    rows = []
    for p in pages:
        try:
            grids = html_to_table_grids(p["html"], page_num=p["page"])
        except Exception:
            continue
        for g in grids:
            for r in g.rows:
                if r.row_type in ("header", "empty"):
                    continue
                row = {
                    "source_type": "table_parser",
                    "page": str(p["page"]),
                    "row_ref": f"t{g.table_index}:r{r.row_index}",
                    "row_type": r.row_type,
                    "name": _slot(g, r, "name"),
                    "spec": _slot(g, r, "spec"),
                    "unit": _slot(g, r, "unit"),
                    "qty": _slot(g, r, "qty"),
                    "unit_price": _slot(g, r, "unit_price"),
                    "unit_price_excl_tax": _slot(g, r, "unit_price_excl_tax"),
                    "total_price": _slot(g, r, "total_price"),
                    "brand": _slot(g, r, "brand"),
                    "remark": _slot(g, r, "remark"),
                    "raw_cells": _raw_cells(r),
                }
                row["flags"] = "|".join(_table_parser_flags(row))
                rows.append(row)
    return rows


# ── Source 2: two_stage rows ──────────────────────────────────────────────────

def _two_stage_flags(row: dict) -> list[str]:
    flags = []
    # two_stage has no price cols — flag if total_price populated (unexpected)
    if row.get("unit_price") or row.get("total_price"):
        flags.append("unexpected_price_col")
    return flags


def _load_two_stage_rows(pdf_path: Path) -> list[dict]:
    cache = _find_two_stage_cache(pdf_path)
    if not cache:
        return []
    rows = []
    with cache.open(encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            row = {
                "source_type": "two_stage",
                "page": r.get("page", ""),
                "row_ref": f"#{r.get('#', '')}",
                "row_type": "quote_line",
                "name": r.get("name", ""),
                "spec": r.get("spec", ""),
                "unit": r.get("unit", ""),
                "qty": r.get("quantity", ""),
                "unit_price": "",
                "unit_price_excl_tax": "",
                "total_price": "",
                "brand": "",
                "remark": r.get("remark", ""),
                "raw_cells": " | ".join(f"{k}={v}" for k, v in r.items() if v and v.strip()),
            }
            row["flags"] = "|".join(_two_stage_flags(row))
            rows.append(row)
    return rows


# ── Source 3: DB quotes ───────────────────────────────────────────────────────

def _db_flags(row: dict) -> list[str]:
    flags = []
    up = _to_float(row.get("unit_price"))
    qty = _to_float(row.get("qty"))
    tp = _to_float(row.get("total_price"))
    name = row.get("name", "")

    # grand_total keyword in DB row — should never be stored
    if _TOTAL_KEYWORDS_RE.search(name):
        flags.append("grand_total_in_db")

    # suspiciously large total (likely OCR parse error)
    if tp and tp > 1_000_000:
        flags.append(f"suspect_total:total={tp:,.0f}")

    # unit_price × qty ≠ total_price (>5% deviation)
    if up and qty and tp:
        calc = round(up * qty, 2)
        if abs(calc - tp) / max(abs(tp), 1) > 0.05:
            flags.append(f"arithmetic_mismatch:calc={calc:.2f}≠db={tp:.2f}")

    return flags


def _load_db_rows(supplier_name: str, project_id: int | None) -> list[dict]:
    if project_id is None:
        return []
    try:
        import sqlite3
        db_path = REPO_ROOT / "data" / "mempas.db"
        con = sqlite3.connect(str(db_path))
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        cur = con.cursor()
        # fuzzy match supplier by name
        cur.execute("SELECT id, name FROM suppliers")
        all_sups = cur.fetchall()
        # collect all fuzzy matches, then pick the one with most quotes in this project
        candidates = [
            sup_id for sup_id, sup_name in all_sups
            if supplier_name in sup_name or sup_name in supplier_name
        ]
        if not candidates:
            print(f"  WARN: supplier '{supplier_name}' not found in DB, skipping DB source")
            return []
        if len(candidates) == 1:
            sid = candidates[0]
        else:
            counts = []
            for cid in candidates:
                n = cur.execute(
                    "SELECT COUNT(*) FROM quotes WHERE project_id=? AND supplier_id=?",
                    (project_id, cid),
                ).fetchone()[0]
                counts.append((n, cid))
            sid = max(counts)[1]
        cur.execute(
            """SELECT q.id, m.standard_name, m.spec, q.unit_price, q.quantity,
                      q.total_price, q.brand, q.remark
               FROM quotes q
               LEFT JOIN materials m ON q.material_id=m.id
               WHERE q.project_id=? AND q.supplier_id=?
               ORDER BY q.id""",
            (project_id, sid),
        )
        rows = []
        for qid, name, spec, up, qty, tp, brand, remark in cur.fetchall():
            row = {
                "source_type": "db",
                "page": "",
                "row_ref": f"qid={qid}",
                "row_type": "quote_line",
                "name": name or "",
                "spec": spec or "",
                "unit": "",
                "qty": str(qty) if qty is not None else "",
                "unit_price": str(up) if up is not None else "",
                "unit_price_excl_tax": "",
                "total_price": str(tp) if tp is not None else "",
                "brand": brand or "",
                "remark": remark or "",
                "raw_cells": "",
            }
            row["flags"] = "|".join(_db_flags(row))
            rows.append(row)
        con.close()
        return rows
    except Exception as e:
        print(f"  WARN: DB error: {e}")
        return []


# ── CSV / file writers ────────────────────────────────────────────────────────

_COMBINED_COLS = [
    "source_type", "page", "row_ref", "row_type",
    "name", "spec", "unit", "qty",
    "unit_price", "unit_price_excl_tax", "total_price",
    "brand", "remark", "flags", "raw_cells",
]


def _write_csv(path: Path, rows: list[dict], cols: list[str] | None = None) -> None:
    if not rows:
        path.write_text("(no data)\n", encoding="utf-8-sig")
        return
    fieldnames = cols or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_tender_csv(path: Path, anchors) -> None:
    rows = []
    for a in anchors:
        canon = a.canonical or {}
        rows.append({
            "seq": a.seq,
            "name": a.name,
            "spec": a.spec,
            "model": a.model,
            "pressure": a.pressure,
            "unit": a.unit,
            "qty": a.qty,
            "profession": a.profession,
            "brand": a.brand,
            "remark": a.remark,
            "canonical_valve_type": canon.get("valve_type", ""),
            "canonical_dn": canon.get("dn", ""),
            "canonical_pn": canon.get("pn", ""),
        })
    _write_csv(path, rows)


# ── per-source stats ──────────────────────────────────────────────────────────

def _source_stats(rows: list[dict], label: str) -> dict:
    ql = [r for r in rows if r.get("row_type") == "quote_line"]
    gt = [r for r in rows if r.get("row_type") == "grand_total"]
    flagged = [r for r in rows if r.get("flags")]
    arith = 0.0
    for r in ql:
        tp = _to_float(r.get("total_price"))
        if tp:
            arith += tp
        else:
            up = _to_float(r.get("unit_price"))
            qty = _to_float(r.get("qty"))
            if up and qty:
                arith += up * qty
    pdf_total = None
    for r in gt:
        v = _to_float(r.get("total_price"))
        if v and v > 0:
            pdf_total = v
            break
    return {
        f"{label}_row_total": len(rows),
        f"{label}_quote_line_count": len(ql),
        f"{label}_grand_total_count": len(gt),
        f"{label}_flagged_count": len(flagged),
        f"{label}_arithmetic_sum": round(arith, 2) if arith else "",
        f"{label}_pdf_declared_total": pdf_total if pdf_total else "",
    }


def _anomaly_summary(tp_rows, ts_rows, db_rows) -> list[str]:
    notes = []
    tp_ql = sum(1 for r in tp_rows if r.get("row_type") == "quote_line")
    ts_ql = len(ts_rows)
    db_ql = sum(1 for r in db_rows if r.get("row_type") == "quote_line")

    if ts_ql > 0 and tp_ql < ts_ql * 0.5:
        notes.append(f"table_parser quote_lines({tp_ql}) far below two_stage({ts_ql}) — likely incomplete HTML parse")
    if db_ql > 0 and tp_ql < db_ql * 0.5:
        notes.append(f"table_parser quote_lines({tp_ql}) far below DB({db_ql})")

    col_misalign = [r for r in tp_rows if "col_misalign" in r.get("flags", "")]
    if col_misalign:
        notes.append(f"table_parser column misalignment in {len(col_misalign)} rows")

    gt_as_ql = [r for r in tp_rows if "grand_total_as_quote_line" in r.get("flags", "")]
    if gt_as_ql:
        notes.append(f"table_parser: {len(gt_as_ql)} grand_total rows mis-classified as quote_line")

    db_suspect = [r for r in db_rows if "suspect_total" in r.get("flags", "")]
    if db_suspect:
        notes.append(f"DB: {len(db_suspect)} rows with suspiciously large total (likely OCR parse error)")

    db_gt = [r for r in db_rows if "grand_total_in_db" in r.get("flags", "")]
    if db_gt:
        notes.append(f"DB: {len(db_gt)} rows with grand_total keywords — grand_total may have been stored as quote")

    gt_count = sum(1 for r in tp_rows if r.get("row_type") == "grand_total")
    if gt_count == 0 and ts_ql > 0:
        notes.append("table_parser: no grand_total row found — PDF declared total not captured")

    return notes if notes else ["no anomalies detected"]


# ── README ────────────────────────────────────────────────────────────────────

_README = """\
# 系统识别事实表审计输出

## 三路来源说明

| 来源 | 可信度 | 说明 |
|---|---|---|
| **two_stage** | 行数最完整 | OCR HTML → LLM 两阶段提取。行数多，但**无价格列**（只有 name/spec/qty），不能用于总价核算 |
| **db** | 价格有问题 | 已入库的 quote 行。行数接近 two_stage，但价格可能因 OCR 列错位而错误（例如泰科龙 unit_price=53946，实为列串位） |
| **table_parser** | 原始最差 | OCR HTML → table_parser 直接解析。行数最少（约为 two_stage 的 1/4~1/5），且存在列错位，**不代表真实报价行** |

## 关键结论

- **报价明细解析当前不可信**：table_parser 行数严重偏少，且存在列错位（如 unit 字段出现"黄铜"等材质文字）。
- **在 table_parser/DB 价格可信之前，不能用 quote_line_count 或比价矩阵 missing 行数判断供应商真实缺报。**
- grand_total 识别：table_parser 只识别出了泰科龙的合计行；绵存/凯硕的"说明：含税总计(元)："格式未被识别（_GRAND_TOTAL_KEYWORDS 正则未覆盖该格式）。
- DB 泰科龙存在单价 ¥53,946、数量 62 的异常行（小计 ¥3,344,664），是 OCR 表格列错位导致的错误入库。

## 文件说明

| 文件 | 内容 |
|---|---|
| `tender_anchors.csv` | 采购清单解析结果（parse_tender_xlsx，可信） |
| `<供应商>_combined_audit.csv` | 三路合并，每行含 source_type + flags，方便人工逐行对照 |
| `compare_summary.csv` | 逐供应商行数/总额/差异汇总，最后一列 anomaly_notes |

## 下一步

在得出任何业务结论前，需先修复：
1. table_parser 的 grand_total 识别（覆盖"说明：含税总计(元)："格式）
2. DB 入库前的列错位检测（unit_price 离群值拦截）
"""


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="系统识别事实表三路对照导出")
    ap.add_argument("--tender", required=True)
    ap.add_argument("--quote", action="append", default=[], metavar="供应商名:PDF路径")
    ap.add_argument("--project-id", type=int, default=None)
    ap.add_argument("--output", default="outputs/recognition_facts")
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # tender
    print(f"\n=== 采购清单 ===")
    anchors = parse_tender_xlsx(args.tender)
    print(f"  {len(anchors)} 条锚点")
    _write_tender_csv(out_dir / "tender_anchors.csv", anchors)

    summary_rows: list[dict] = []

    for spec in args.quote:
        if ":" not in spec:
            continue
        name, pdf_str = spec.split(":", 1)
        name, pdf_path = name.strip(), Path(pdf_str.strip())
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)

        print(f"\n=== {name} ===")

        tp_rows = _load_table_parser_rows(pdf_path)
        ts_rows = _load_two_stage_rows(pdf_path)
        db_rows = _load_db_rows(name, args.project_id)

        tp_ql = sum(1 for r in tp_rows if r["row_type"] == "quote_line")
        tp_gt = sum(1 for r in tp_rows if r["row_type"] == "grand_total")
        print(f"  table_parser : {tp_ql} quote_line, {tp_gt} grand_total, {len(tp_rows)} total")
        print(f"  two_stage    : {len(ts_rows)} rows (no price cols)")
        print(f"  db           : {len(db_rows)} rows")

        # combined
        combined = tp_rows + ts_rows + db_rows
        _write_csv(out_dir / f"{safe}_combined_audit.csv", combined, _COMBINED_COLS)

        # stats
        tp_stats = _source_stats(tp_rows, "tp")
        ts_stats = _source_stats(ts_rows, "ts")
        db_stats = _source_stats(db_rows, "db")
        anomalies = _anomaly_summary(tp_rows, ts_rows, db_rows)

        row = {"supplier": name, **tp_stats, **ts_stats, **db_stats,
               "anomaly_notes": " // ".join(anomalies)}
        summary_rows.append(row)

        for note in anomalies:
            lvl = "ERROR" if any(k in note for k in ("far below", "large total", "grand_total stored")) else "WARN"
            print(f"  {lvl}: {note}")

    _write_csv(out_dir / "compare_summary.csv", summary_rows)
    (out_dir / "README.md").write_text(_README, encoding="utf-8")
    print(f"\n→ {out_dir.resolve()}")
    print("   compare_summary.csv  |  <供应商>_combined_audit.csv  |  README.md")


if __name__ == "__main__":
    main()
