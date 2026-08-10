"""Verify row-count vs amount drift across kaishuo & taikelong snapshots (read-only).

Confirms (or refutes) the claim:
  - kaishuo: NO row drift (all ~89/89); 702k/984k/932k = tax-field / column drift.
  - taikelong: REAL row drift (77 / 84 / 89).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]


def items_of(snap):
    out = []
    for v in snap.get("llm", {}).values():
        d = v.get("data") or {}
        if isinstance(d, dict):
            out.extend(d.get("items", []) or [])
    return out


def analyze(fname):
    snap = json.loads((ROOT / "outputs" / fname).read_text("utf-8"))
    items = items_of(snap)
    seqs = sorted({int(i["seq"]) for i in items if str(i.get("seq")).isdigit()})
    n_rows = len(items)
    # doc incl total
    tot = 0.0
    incl_present = 0
    for it in items:
        v = it.get("total_price_incl_tax")
        if v is None:
            iu, q = it.get("unit_price_incl_tax"), it.get("qty")
            if iu is not None and q is not None:
                v = iu * q
        if v is not None:
            tot += v
        if it.get("unit_price_incl_tax") is not None or it.get("total_price_incl_tax") is not None:
            incl_present += 1
    return {
        "rows": n_rows,
        "uniq_seq": len(seqs),
        "seq_range": (min(seqs), max(seqs)) if seqs else None,
        "incl_present": incl_present,
        "doc_total": tot,
    }


for tag in ("kaishuo", "taikelong"):
    print("=" * 80)
    print(tag.upper())
    print("=" * 80)
    files = sorted(p.name for p in (ROOT / "outputs").glob(f"fresh_snap_*_quote_{tag}.json"))
    print(f"  {'run':<12}{'rows':<6}{'uniq_seq':<10}{'seq_range':<12}{'incl_present':<14}{'doc_total':<14}")
    for f in files:
        rid = f.replace("fresh_snap_", "").replace(f"_quote_{tag}.json", "")
        try:
            a = analyze(f)
        except Exception as e:
            print(f"  {rid:<12} ERROR {e}")
            continue
        sr = f"{a['seq_range'][0]}-{a['seq_range'][1]}" if a["seq_range"] else "-"
        print(f"  {rid:<12}{a['rows']:<6}{a['uniq_seq']:<10}{sr:<12}"
              f"{a['incl_present']:<14}{a['doc_total']:<14.2f}")
    print()
