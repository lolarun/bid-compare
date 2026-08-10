"""audit_golden_pdf.py — 半自动风险型 PDF 审计。

对 golden Excel 中的高风险行，渲染 PDF 原页面并输出审计清单，
供视觉核对 PDF 原值 vs golden 值。

审计样本选取规则：
1. 当前 baseline diff 中所有偏差行
2. 按含税合价降序累计贡献前 80% 的行
3. 每份文件首/中/尾页至少各取若干行
4. 每种表格版式至少 5 行
5. 所有疑似列错位、税价混淆和名称 OCR 纠错行

输出：
  outputs/golden_audit_images/<doc>/page_<N>.png  — 整页渲染
  outputs/golden_audit_images/audit_manifest.json — 审计清单

用法：
    python scripts/audit_golden_pdf.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DOCS = REPO / "docs" / "test"
DIFF_DIR = REPO / "outputs" / "e2e_diff"
SNAP_DIR = REPO / "tests" / "fixtures" / "ocr_snapshots"
OUT_DIR = REPO / "outputs" / "golden_audit_images"

DOC_CFG = {
    "quote_taikelong": {
        "pdf": DOCS / "泰科龙投标文件.pdf",
        "xlsx": DOCS / "泰科龙投标清单.xlsx",
        "declared_total": 1_067_616.41,
        "format": "transposed",
    },
    "quote_miancun": {
        "pdf": DOCS / "上海绵存投标文件.pdf",
        "xlsx": DOCS / "上海绵存投标清单.xlsx",
        "declared_total": 1_667_051.0,
        "format": "horizontal",
    },
    "quote_kaishuo": {
        "pdf": DOCS / "凯硕新正投标文件.pdf",
        "xlsx": DOCS / "凯硕新正投标清单.xlsx",
        "declared_total": 932_154.0,
        "format": "horizontal",
    },
}


def _load_golden_rows(xlsx: Path) -> list[dict]:
    from scripts.audit_golden import _load_rows
    _sheet, _h, _present, data_rows, _tot = _load_rows(xlsx)
    return data_rows


def _load_diff_rows(doc_name: str) -> list[dict]:
    csv_path = DIFF_DIR / doc_name / "row_diff.csv"
    if not csv_path.exists():
        return []
    rows = []
    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def _get_extracted_page_map(doc_name: str, pdf_path: Path) -> dict[str, int]:
    """Replay snapshot to get seq → page mapping from extracted data."""
    snap = SNAP_DIR / f"{doc_name}.json"
    if not snap.exists():
        return {}
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    provider = SnapshotProvider(None, snap, mode="replay")
    draft = recognize_tables(str(pdf_path), provider, _get_quote_adapter())

    page_map = {}
    for r in draft.rows:
        if r.row_type != "quote_line":
            continue
        seq = str(r.fields.get("seq") or "").strip()
        if seq.isdigit() and r.source_ref:
            page_map[seq] = r.source_ref.page
    return page_map


def _get_pdf_page_count(pdf_path: Path) -> int:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    n = len(pdf)
    pdf.close()
    return n


def _select_samples(doc_name: str, cfg: dict) -> list[dict]:
    """Select audit samples based on risk criteria. Returns list of
    {seq, page, reason, golden_fields}."""

    golden_rows = _load_golden_rows(cfg["xlsx"])
    diff_rows = _load_diff_rows(doc_name)
    page_map = _get_extracted_page_map(doc_name, cfg["pdf"])
    total_pages = _get_pdf_page_count(cfg["pdf"])

    golden_by_seq = {str(r["seq"]): r for r in golden_rows if str(r.get("seq", "")).strip().isdigit()}

    samples: dict[str, dict] = {}  # seq → sample

    def _add(seq: str, reason: str):
        if seq not in golden_by_seq:
            return
        if seq in samples:
            samples[seq]["reasons"].append(reason)
        else:
            g = golden_by_seq[seq]
            samples[seq] = {
                "seq": seq,
                "page": page_map.get(seq),
                "reasons": [reason],
                "golden": {
                    "name": g.get("name"),
                    "spec": g.get("spec"),
                    "model": g.get("model"),
                    "unit": g.get("unit"),
                    "qty": g.get("qty"),
                    "unit_price_excl_tax": g.get("unit_price_excl_tax"),
                    "unit_price_incl_tax": g.get("unit_price_incl_tax"),
                    "total_price_excl_tax": g.get("total_price_excl_tax"),
                    "total_price_incl_tax": g.get("total_price_incl_tax"),
                    "tax_rate": g.get("tax_rate"),
                    "tax_amount": g.get("tax_amount"),
                    "brand": g.get("brand"),
                },
            }

    # ── Criterion 1: all diff rows ──
    diff_seqs = set()
    for d in diff_rows:
        s = d.get("seq", "")
        if s:
            diff_seqs.add(s)
            kind = d.get("kind", "")
            field = d.get("field", "")
            _add(s, f"diff:{field}:{kind}")

    # ── Criterion 2: top 80% by total_price_incl_tax cumulative ──
    sorted_rows = sorted(
        golden_rows,
        key=lambda r: abs(r.get("total_price_incl_tax") or 0),
        reverse=True,
    )
    total_sum = sum(abs(r.get("total_price_incl_tax") or 0) for r in golden_rows)
    cumsum = 0.0
    for r in sorted_rows:
        seq = str(r.get("seq", "")).strip()
        if not seq.isdigit():
            continue
        tp = abs(r.get("total_price_incl_tax") or 0)
        cumsum += tp
        _add(seq, "top80pct_value")
        if cumsum >= total_sum * 0.80:
            break

    # ── Criterion 3: first/middle/last page rows ──
    pages_with_rows: dict[int, list[str]] = {}
    for seq, page in page_map.items():
        pages_with_rows.setdefault(page, []).append(seq)

    if pages_with_rows:
        all_pages = sorted(pages_with_rows.keys())
        first_p = all_pages[0]
        last_p = all_pages[-1]
        mid_p = all_pages[len(all_pages) // 2]
        for p in [first_p, mid_p, last_p]:
            for seq in pages_with_rows.get(p, [])[:3]:
                _add(seq, f"page_coverage:{p}")
    else:
        # No page map (e.g. miancun) — sample first/mid/last seqs
        all_seqs = sorted(golden_by_seq.keys(), key=int)
        if all_seqs:
            for s in [all_seqs[0], all_seqs[len(all_seqs)//2], all_seqs[-1]]:
                _add(s, "seq_coverage")

    # ── Criterion 4: each format type at least 5 rows ──
    fmt = cfg.get("format", "horizontal")
    fmt_count = sum(1 for s in samples.values() if any("format" in r for r in s["reasons"]))
    if fmt_count < 5:
        all_seqs = sorted(golden_by_seq.keys(), key=int)
        for s in all_seqs:
            if s not in samples:
                _add(s, f"format_sample:{fmt}")
            if len([s2 for s2 in samples.values()
                    if any("format_sample" in r for r in s2["reasons"])]) >= 5:
                break

    # ── Criterion 5: suspected column-shift / tax confusion / OCR correction ──
    for d in diff_rows:
        s = d.get("seq", "")
        field = d.get("field", "")
        kind = d.get("kind", "")
        if not s:
            continue
        # Column shift: qty or spec wrong, or large price error
        if field in ("qty", "spec") and kind == "num":
            _add(s, "suspect:column_shift")
        if field in ("unit_price_excl_tax", "unit_price_incl_tax") and kind == "num":
            try:
                ae = float(d.get("abs_err", 0))
                gv = abs(float(d.get("golden", 1)))
                if gv > 0 and ae / gv > 0.1:
                    _add(s, "suspect:price_confusion")
            except (ValueError, TypeError):
                pass
        if field == "name" and kind == "str":
            _add(s, "suspect:ocr_name_error")

    # Ensure minimum 5 samples per doc
    if len(samples) < 5:
        for s in sorted(golden_by_seq.keys(), key=int):
            if s not in samples:
                _add(s, "minimum_coverage")
            if len(samples) >= 5:
                break

    return sorted(samples.values(), key=lambda x: int(x["seq"]))


def _render_pages(pdf_path: Path, pages: set[int], out_dir: Path, scale: float = 2.5):
    """Render specific PDF pages to PNG images."""
    import pypdfium2 as pdfium

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for p in sorted(pages):
            idx = p - 1
            if idx < 0 or idx >= len(pdf):
                continue
            out_path = out_dir / f"page_{p:03d}.png"
            if out_path.exists():
                continue
            page = pdf[idx]
            pil_img = page.render(scale=scale).to_pil().convert("RGB")
            pil_img.save(str(out_path), "PNG")
            print(f"  rendered page {p} → {out_path.name}")
    finally:
        pdf.close()


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}

    for doc_name, cfg in DOC_CFG.items():
        pdf_path = Path(cfg["pdf"])
        if not pdf_path.exists():
            print(f"[skip] {doc_name}: PDF not found")
            continue

        print(f"\n[{doc_name}] selecting audit samples...")
        samples = _select_samples(doc_name, cfg)
        print(f"  → {len(samples)} samples selected")

        # Collect pages to render
        pages_needed = set()
        for s in samples:
            if s["page"]:
                pages_needed.add(s["page"])

        # For samples without page info, try to estimate from neighbors
        if not pages_needed and samples:
            # No page map at all — render a spread of pages
            total = _get_pdf_page_count(pdf_path)
            spread = [1, total // 4, total // 2, 3 * total // 4, total]
            pages_needed = set(p for p in spread if 1 <= p <= total)

        print(f"  rendering {len(pages_needed)} pages...")
        doc_dir = OUT_DIR / doc_name
        _render_pages(pdf_path, pages_needed, doc_dir)

        manifest[doc_name] = {
            "pdf": str(pdf_path),
            "format": cfg.get("format"),
            "declared_total": cfg["declared_total"],
            "sample_count": len(samples),
            "pages_rendered": sorted(pages_needed),
            "samples": samples,
        }

    manifest_path = OUT_DIR / "audit_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ manifest: {manifest_path.relative_to(REPO)}")

    # Summary
    for doc_name, m in manifest.items():
        print(f"\n{doc_name}: {m['sample_count']} samples, {len(m['pages_rendered'])} pages")
        reason_counts: dict[str, int] = {}
        for s in m["samples"]:
            for r in s["reasons"]:
                tag = r.split(":")[0]
                reason_counts[tag] = reason_counts.get(tag, 0) + 1
        for tag, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {tag}: {cnt}")


if __name__ == "__main__":
    main()
