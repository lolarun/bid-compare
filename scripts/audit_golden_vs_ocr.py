"""audit_golden_vs_ocr.py — Step 0 只读三方对账（dry-run，绝不改数据）。

目的（CLAUDE.md §11/§12/§13）：在任何 golden 校准或系统优化之前，先把
「PDF OCR 物理行 ↔ Excel 标准答案 ↔ 当前 golden」三方逐行摊开，暴露：
  - PDF 有但 Excel/golden 没有的物理行（疑似缺行或 PDF 额外内容）；
  - Excel/golden 有但 PDF OCR 找不到的行（疑似 Excel 误录或 PDF 漏识别）；
  - 三方按 (spec + 含税合价) 能对上、但名称不同的行（规范化 or 误标）；
  - OCR 跨页重叠（同一行在相邻页重复出现）。

强约束：
  - 只读。不写任何文件，不改 golden，不打真实 API（用已有 snapshot replay）。
  - 不下「谁对谁错」结论——把证据摊开，由人对照真实 PDF 裁决。

用法：
    python scripts/audit_golden_vs_ocr.py            # 默认 quote_miancun
    python scripts/audit_golden_vs_ocr.py <doc_name> # 指定 baseline doc
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 确保中文输出不被 GBK 截断
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.disable(logging.CRITICAL)

# doc_name → (pdf, excel, golden)
DOCS = {
    "quote_miancun": (
        REPO / "docs/test/上海绵存投标文件.pdf",
        REPO / "docs/test/上海绵存投标清单.xlsx",
        REPO / "data/golden/quote_miancun.json",
    ),
    "quote_kaishuo": (
        REPO / "docs/test/凯硕新正投标文件.pdf",
        REPO / "docs/test/凯硕新正投标清单.xlsx",
        REPO / "data/golden/quote_kaishuo.json",
    ),
    "quote_taikelong": (
        REPO / "docs/test/泰科龙投标文件.pdf",
        REPO / "docs/test/泰科龙投标清单.xlsx",
        REPO / "data/golden/quote_taikelong.json",
    ),
}


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("，", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(s) -> str:
    return "".join(str(s or "").split()).lower()


# ── 1. Excel 标准答案行 ─────────────────────────────────────────────────────

def load_excel_rows(xlsx: Path) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(str(xlsx), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c or "").strip() for c in rows[0]]
    # locate columns by header keyword; `exclude` skips headers containing those substrings
    def col(keys, exclude=()):
        for k in keys:                       # priority by key order, exact-ish first
            for i, h in enumerate(header):
                if k in h and not any(x in h for x in exclude):
                    return i
        return None
    ci_name = col(["项目名称", "名称", "品名"])
    ci_spec = col(["规格"])
    ci_qty = col(["数量"])
    # 含税合价：必须取「价税合计/含税合计」，绝不能落到「合计(不含税)」
    ci_total = col(["价税合计", "含税合计", "含税合价"], exclude=["不含税"])
    out = []
    for r in rows[1:]:
        if ci_name is None or r[ci_name] in (None, ""):
            continue
        name = str(r[ci_name]).strip()
        if name in ("合计", "总计", "小计"):
            continue
        out.append({
            "name": name,
            "spec": str(r[ci_spec]).strip() if ci_spec is not None and r[ci_spec] else "",
            "qty": _num(r[ci_qty]) if ci_qty is not None else None,
            "total": _num(r[ci_total]) if ci_total is not None else None,
        })
    return out


# ── 2. golden 行 ────────────────────────────────────────────────────────────

def load_golden_rows(path: Path) -> list[dict]:
    g = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in g["rows"]:
        out.append({
            "name": str(r.get("name") or "").strip(),
            "spec": str(r.get("spec") or "").strip(),
            "qty": _num(r.get("qty")),
            "total": _num(r.get("total_price_incl_tax") or r.get("total_price")),
        })
    return out


# ── 3. PDF OCR 物理行（按页，标注跨页重叠）────────────────────────────────────

def load_ocr_rows(pdf: Path, snap: Path) -> list[dict]:
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.document_loader import DocumentLoader
    provider = SnapshotProvider(None, snap, mode="replay")
    imgs = DocumentLoader.to_images(str(pdf))
    ocr = provider._ocr

    def h(b):
        return hashlib.sha256(b).hexdigest()

    rows: list[dict] = []
    for pageno in range(1, len(imgs) + 1):
        html = ocr.get(h(imgs[pageno - 1]), "")
        if not html:
            continue
        for tr in re.split(r"<tr[^>]*>", html):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            txt = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            txt = [t for t in txt if t]
            if len(txt) < 4:
                continue
            # heuristic: a quote row has a name (non-numeric first cell) + a DN spec + numbers
            name = txt[0]
            if _num(name) is not None:
                continue
            spec = next((t for t in txt if re.match(r"^DN\d+", t, re.I)), "")
            nums = [_num(t) for t in txt if _num(t) is not None]
            total = nums[-1] if nums else None
            if not spec or total is None:
                continue
            rows.append({"page": pageno, "name": name, "spec": spec,
                         "qty": nums[-3] if len(nums) >= 3 else None, "total": total})
    # mark cross-page overlap: same (name,spec,total) appearing on >1 page or twice
    seen: dict[tuple, int] = {}
    for r in rows:
        key = (_norm(r["name"]), _norm(r["spec"]), r["total"])
        seen[key] = seen.get(key, 0) + 1
    for r in rows:
        key = (_norm(r["name"]), _norm(r["spec"]), r["total"])
        r["dup_count"] = seen[key]
    return rows


# ── 三方对账：按 (spec + total) 强键匹配 ────────────────────────────────────

def key_st(r) -> tuple:
    return (_norm(r["spec"]), r["total"])


def main():
    doc = sys.argv[1] if len(sys.argv) > 1 else "quote_miancun"
    if doc not in DOCS:
        sys.exit(f"unknown doc: {doc}. choices: {list(DOCS)}")
    pdf, xlsx, golden = DOCS[doc]
    snap = REPO / "tests/fixtures/ocr_snapshots" / f"{doc}.json"

    print(f"{'='*72}\nStep 0 三方对账 (dry-run, 只读): {doc}\n{'='*72}")
    print(f"PDF:    {pdf.name}")
    print(f"Excel:  {xlsx.name}")
    print(f"Golden: {golden.name}\n")

    excel = load_excel_rows(xlsx)
    gold = load_golden_rows(golden)
    ocr = load_ocr_rows(pdf, snap)
    # OCR physical rows: collapse cross-page duplicates to unique physical rows
    ocr_unique: dict[tuple, dict] = {}
    for r in ocr:
        k = (_norm(r["name"]), _norm(r["spec"]), r["total"])
        if k not in ocr_unique:
            ocr_unique[k] = r
    ocr_u = list(ocr_unique.values())

    print(f"Excel 标准答案行: {len(excel)}")
    print(f"Golden 行:        {len(gold)}")
    print(f"OCR 物理行(原始): {len(ocr)}  | 去跨页重叠后唯一: {len(ocr_u)}")

    # 1) golden vs excel 一致性（golden 应忠实于 Excel）
    ex_keys = {key_st(r) for r in excel}
    g_keys = {key_st(r) for r in gold}
    print(f"\n── [A] Golden ⟷ Excel 一致性 (按 spec+含税合价) ──")
    print(f"  Excel 独有: {len(ex_keys - g_keys)}   Golden 独有: {len(g_keys - ex_keys)}")
    if ex_keys - g_keys or g_keys - ex_keys:
        print("  ⚠ golden 与 Excel 不完全一致（预期应一致）")

    # 2) OCR 物理行 vs Excel
    print(f"\n── [B] OCR 物理行 按 (spec+含税合价) 匹配 Excel ──")
    matched, ocr_extra = [], []
    ex_by_key: dict[tuple, list] = {}
    for r in excel:
        ex_by_key.setdefault(key_st(r), []).append(r)
    used = set()
    for r in ocr_u:
        k = key_st(r)
        cand = [e for e in ex_by_key.get(k, []) if id(e) not in used]
        if cand:
            e = cand[0]
            used.add(id(e))
            matched.append((r, e))
        else:
            ocr_extra.append(r)
    excel_unmatched = [e for e in excel if id(e) not in used]

    print(f"  OCR↔Excel 匹配: {len(matched)}")
    print(f"  OCR 有 / Excel 无 (PDF 额外物理行): {len(ocr_extra)}")
    for r in ocr_extra:
        print(f"    [p{r['page']}] {r['name'][:18]:<18} {r['spec']:<8} qty={r['qty']} total={r['total']} (跨页出现×{r['dup_count']})")
    print(f"  Excel 有 / OCR 无 (标准答案有但 PDF 未识别到): {len(excel_unmatched)}")
    for e in excel_unmatched:
        print(f"    {e['name'][:18]:<18} {e['spec']:<8} qty={e['qty']} total={e['total']}")

    # 3) 名称分歧：spec+total 对上但名称不同（规范化 or 误标）
    print(f"\n── [C] 匹配行中名称分歧 (spec+合价相同, 名称不同) ──")
    name_div = [(r, e) for r, e in matched if _norm(r["name"]) != _norm(e["name"])]
    print(f"  共 {len(name_div)} 行名称不同：")
    for r, e in name_div:
        print(f"    [p{r['page']}] OCR='{r['name']}'  ⟷  Excel='{e['name']}'  ({r['spec']} total={r['total']})")

    # 4) OCR 跨页重叠
    overlap = [r for r in ocr if r["dup_count"] > 1]
    print(f"\n── [D] OCR 跨页/重复行 (同 name+spec+total 出现 >1 次) ──")
    seen_o = set()
    for r in overlap:
        k = (_norm(r["name"]), _norm(r["spec"]), r["total"])
        if k in seen_o:
            continue
        seen_o.add(k)
        print(f"    {r['name'][:18]:<18} {r['spec']:<8} total={r['total']}  出现 {r['dup_count']} 次")

    print(f"\n{'='*72}\n结论需人工对照真实 PDF 裁决，本脚本不改任何数据。\n{'='*72}")


if __name__ == "__main__":
    main()
