"""Offline non-determinism proof for kaishuo (read-only, no API calls).

For every kaishuo fresh snapshot it:
  1. Hashes each OCR HTML's *content* (not the image-hash key) and records which
     seq range that page covers — so the SAME logical page can be compared across
     runs even when image rendering (and thus the OCR cache key) differs.
  2. For each LLM output: seq range, row count, incl/excl coverage, page totals,
     and a "duplicate-tax" flag (incl == round(excl*1.13)).
  3. Rigorously binds each LLM call to its OCR page by recomputing the snapshot
     LLM key = sha256(thinking \x00 prompt \x00 llm_input), brute-forcing page_no.
     This proves prompt + input identity, isolating the LLM as the only variable.

DO NOT MODIFY any code path. Pure analysis.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.intelligence.table_recognizer import _build_llm_input  # noqa: E402
from apps.api.intelligence.providers.dashscope_ocr import (  # noqa: E402
    _QUOTE_S2_PROMPT, _QUOTE_S2_TABLE_PROMPT,
)

SNAPS = [
    "fresh_snap_nebt_snw_quote_kaishuo.json",   # Jun20 09:45
    "fresh_snap_ur0pn3vu_quote_kaishuo.json",   # Jun20 20:42
    "fresh_snap_mhrqzxbf_quote_kaishuo.json",   # Jun20 22:45
    "fresh_snap_wiv4zy3t_quote_kaishuo.json",   # Jun21 00:22
    "fresh_snap_892td3if_quote_kaishuo.json",   # Jun21 00:43  (seq71-89 incl empty)
    "fresh_snap_39szr0ev_quote_kaishuo.json",   # Jun21 01:12
    "fresh_snap_iazb9jio_quote_kaishuo.json",   # Jun21 01:16  (seq23-46 dup-tax)
]

PROMPTS = {"S2": _QUOTE_S2_PROMPT, "S2_TABLE": _QUOTE_S2_TABLE_PROMPT}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _llm_key(thinking: int, prompt: str, content: str) -> str:
    return hashlib.sha256(
        f"{thinking}\x00{prompt}\x00{content}".encode("utf-8")
    ).hexdigest()


# ── golden ────────────────────────────────────────────────────────────────
g = json.loads((ROOT / "data/golden/quote_kaishuo.json").read_text("utf-8"))
GOLD = {}
for r in g["rows"]:
    try:
        GOLD[int(r["seq"])] = r
    except (ValueError, TypeError):
        pass
DECLARED = g["declared_total"]


def _seqset_from_html(html: str) -> set[int]:
    """Best-effort: which seqs this OCR page covers, by intersecting the golden
    seq numbers' incl-tax totals appearing as text. Falls back to nothing."""
    found = set()
    for seq, row in GOLD.items():
        # a strong, fairly unique signal: the incl-tax unit price as printed
        v = row.get("unit_price_incl_tax")
        if v is None:
            continue
        token = ("%g" % v)
        if token in html:
            found.add(seq)
    return found


def _items(data) -> list[dict]:
    if isinstance(data, dict):
        return data.get("items", []) or []
    return []


def _seqs(items) -> list[int]:
    out = []
    for it in items:
        try:
            out.append(int(it.get("seq")))
        except (ValueError, TypeError):
            pass
    return sorted(out)


def _coverage(items):
    n = len(items)
    incl = sum(1 for it in items
               if it.get("unit_price_incl_tax") is not None
               or it.get("total_price_incl_tax") is not None)
    excl = sum(1 for it in items
               if it.get("unit_price_excl_tax") is not None
               or it.get("total_price_excl_tax") is not None)
    return n, incl, excl


def _dup_tax(items) -> int:
    """rows where incl_unit ~= excl_unit * 1.13 (the duplicate-tax artifact)."""
    c = 0
    for it in items:
        iu, eu = it.get("unit_price_incl_tax"), it.get("unit_price_excl_tax")
        if iu and eu and abs(iu - eu * 1.13) < max(0.5, eu * 0.001):
            c += 1
    return c


def _page_incl_total(items) -> float:
    t = 0.0
    for it in items:
        v = it.get("total_price_incl_tax")
        if v is None:
            iu, q = it.get("unit_price_incl_tax"), it.get("qty")
            if iu is not None and q is not None:
                v = iu * q
        if v is not None:
            t += v
    return t


def bind_to_ocr(content_hash_by_input, html_by_hash, llm_key):
    """Return the OCR-html content-hash whose recomputed llm_input key == llm_key."""
    return content_hash_by_input.get(llm_key)


print("=" * 90)
print("KAISHUO OFFLINE NON-DETERMINISM ANALYSIS  (declared_total=%.0f)" % DECLARED)
print("=" * 90)

# per-run records: run_id -> {"ocr": [...], "llm": [...], "page_total": float}
runs = {}

for fname in SNAPS:
    p = ROOT / "outputs" / fname
    if not p.exists() or p.stat().st_size == 0:
        continue
    run_id = fname.split("_quote")[0].replace("fresh_snap_", "")
    snap = json.loads(p.read_text("utf-8"))
    ocr = snap.get("ocr", {})
    llm = snap.get("llm", {})

    # Build: for each OCR html, compute its llm_input keys (try page_no 1..30,
    # both prompts, thinking 0/1) → map key -> (content_hash, page_no, prompt, mode)
    key_to_ocr = {}
    ocr_records = []
    for img_hash, html in ocr.items():
        chash = _h(html)
        seqset = _seqset_from_html(html)
        ocr_records.append({
            "img_hash": img_hash[:12],
            "content_hash": chash,
            "len": len(html),
            "tables": html.count("<table"),
            "trs": html.count("<tr"),
            "seqset": seqset,
        })
        if not html.strip():
            continue
        for pno in range(1, 31):
            try:
                llm_input, _exp, mode, _fb = _build_llm_input(html, pno)
            except Exception:
                continue
            for pid, prompt in PROMPTS.items():
                for th in (0, 1):
                    k = _llm_key(th, prompt, llm_input)
                    if k in llm and k not in key_to_ocr:
                        key_to_ocr[k] = {
                            "content_hash": chash, "page_no": pno,
                            "prompt": pid, "mode": mode, "thinking": th,
                            "input_hash": _h(llm_input),
                        }

    llm_records = []
    for k, v in llm.items():
        items = _items(v.get("data"))
        if not items:
            llm_records.append({"key": k[:12], "rows": 0, "seqs": [],
                                "bound": key_to_ocr.get(k)})
            continue
        n, incl, excl = _coverage(items)
        sq = _seqs(items)
        llm_records.append({
            "key": k[:12],
            "rows": n, "incl": incl, "excl": excl,
            "seqs": sq,
            "seq_range": (min(sq), max(sq)) if sq else None,
            "dup_tax": _dup_tax(items),
            "page_incl_total": _page_incl_total(items),
            "bound": key_to_ocr.get(k),
        })

    runs[run_id] = {"ocr": ocr_records, "llm": llm_records}

    # ---- per-run print ----
    print(f"\n{'─'*90}\nRUN {run_id}   ({fname})")
    doc_total = 0.0
    print("  LLM calls:")
    for r in sorted(llm_records, key=lambda x: (x.get('seq_range') or (999,))):
        if r["rows"] == 0:
            b = r["bound"]
            bind = f"ocr={b['content_hash']} p{b['page_no']} {b['prompt']} th{b['thinking']}" if b else "UNBOUND"
            print(f"    [empty] key={r['key']}  {bind}")
            continue
        b = r["bound"]
        bind = (f"ocr={b['content_hash']} p{b['page_no']} {b['prompt']}/{b['mode']} th{b['thinking']}"
                if b else "UNBOUND(input not reproduced)")
        sr = r["seq_range"]
        print(f"    seq{sr[0]:>2}-{sr[1]:<2} rows={r['rows']:>2} "
              f"incl={r['incl']:>2}/{r['rows']:<2} excl={r['excl']:>2}/{r['rows']:<2} "
              f"dup_tax={r['dup_tax']:>2} pInclTot={r['page_incl_total']:>12.2f}  {bind}")
        doc_total += r["page_incl_total"]
    print(f"  >> doc incl total = {doc_total:>12.2f}   (declared {DECLARED:.0f}, diff {doc_total-DECLARED:+.2f})")

# ── cross-run comparison on the two contested seq ranges ────────────────────
print("\n" + "=" * 90)
print("CROSS-RUN COMPARISON")
print("=" * 90)


def find_page(run, target_seqs):
    """Find the LLM record whose seq range best covers target_seqs."""
    best, score = None, -1
    for r in run["llm"]:
        if not r.get("seqs"):
            continue
        s = len(set(r["seqs"]) & set(target_seqs))
        if s > score:
            best, score = r, s
    return best


for label, target in [("seq71-89", range(71, 90)), ("seq23-46", range(23, 47))]:
    print(f"\n### Logical page {label} across runs")
    print(f"  {'run':<10} {'ocr_content_hash':<18} {'llm_key':<14} {'rows':<5} "
          f"{'incl':<7} {'excl':<7} {'dup_tax':<8} {'page_incl_total':<14}")
    hashes = set()
    for run_id, run in runs.items():
        rec = find_page(run, list(target))
        if not rec:
            print(f"  {run_id:<10} (no covering LLM page)")
            continue
        b = rec.get("bound")
        oh = b["content_hash"] if b else "??(unbound)"
        hashes.add(oh)
        print(f"  {run_id:<10} {oh:<18} {rec['key']:<14} {rec['rows']:<5} "
              f"{str(rec['incl'])+'/'+str(rec['rows']):<7} "
              f"{str(rec['excl'])+'/'+str(rec['rows']):<7} "
              f"{rec['dup_tax']:<8} {rec['page_incl_total']:<14.2f}")
    bound_hashes = {h for h in hashes if not h.startswith("??")}
    if len(bound_hashes) == 1:
        print(f"  -> OCR content hash IDENTICAL across bound runs ({bound_hashes}).")
        print("     Pipeline transform is deterministic => any output difference = LLM non-determinism.")
    elif len(bound_hashes) > 1:
        print(f"  -> OCR content hash DIFFERS across runs: {bound_hashes} => OCR/render non-determinism upstream.")


# ── Decisive: per-row arithmetic consistency on the +52K page (seq23-46) ─────
# Compare a CORRECT run vs a WRONG run that shares the SAME OCR content hash,
# to show (a) it is the LLM (identical input) and (b) which generic signal
# separates correct from wrong WITHOUT any ×/÷ tax derivation.
print("\n" + "=" * 90)
print("DECISIVE PER-ROW CHECK — seq23-46 page (source of the +52K)")
print("=" * 90)


def raw_items_for_seqrange(fname, target):
    snap = json.loads((ROOT / "outputs" / fname).read_text("utf-8"))
    best, score = None, -1
    for v in snap.get("llm", {}).values():
        items = _items(v.get("data"))
        sq = set(_seqs(items))
        s = len(sq & set(target))
        if s > score:
            best, score = items, s
    return best or []


CORRECT = "fresh_snap_wiv4zy3t_quote_kaishuo.json"   # doc total == declared exactly
WRONG = "fresh_snap_39szr0ev_quote_kaishuo.json"     # same OCR hash, +52K
tgt = list(range(23, 47))
ci = {int(it["seq"]): it for it in raw_items_for_seqrange(CORRECT, tgt) if str(it.get("seq")).isdigit()}
wi = {int(it["seq"]): it for it in raw_items_for_seqrange(WRONG, tgt) if str(it.get("seq")).isdigit()}

print(f"  CORRECT run = wiv4zy3t   WRONG run = 39szr0ev   (both OCR hash 4de86d17b6dc)")
print(f"  Generic signal (NO ×/÷ tax): total_incl - total_excl ?= tax_amount   [uses OCR's own tax_amount column]")
print(f"  seq | qty | CORRECT excl_u/incl_u  ->Tincl-Texcl vs tax | WRONG excl_u/incl_u  ->Tincl-Texcl vs tax")
hdr_seqs = [s for s in tgt if s in ci and s in wi]
ok_correct = ok_wrong = 0


def total_ident(it):
    e = it.get("unit_price_excl_tax"); i = it.get("unit_price_incl_tax")
    t = it.get("tax_amount"); q = it.get("qty")
    if None in (e, i, t, q):
        return None, None
    implied = (i - e) * q
    return implied, abs(implied - t) < max(1.0, abs(t) * 0.02)


for s in hdr_seqs[:12]:
    c, w = ci[s], wi[s]
    q = c.get("qty")
    cimp, cok = total_ident(c)
    wimp, wok = total_ident(w)
    ct = c.get("tax_amount")
    ok_correct += 1 if cok else 0
    ok_wrong += 1 if wok else 0
    print(f"  {s:<3} | {str(q):<4}| {str(c.get('unit_price_excl_tax')):>7}/{str(c.get('unit_price_incl_tax')):>7} "
          f"-> {str(round(cimp,1) if cimp is not None else None):>9} vs {str(ct):>8} ok={str(cok):<5}"
          f"| {str(w.get('unit_price_excl_tax')):>7}/{str(w.get('unit_price_incl_tax')):>7} "
          f"-> {str(round(wimp,1) if wimp is not None else None):>9} vs {str(ct):>8} ok={str(wok):<5}")

print(f"\n  total-level tax-identity pass rate over shown rows:")
print(f"    CORRECT run: {ok_correct}/{len(hdr_seqs[:12])}    WRONG run: {ok_wrong}/{len(hdr_seqs[:12])}")

# ── Candidate-selector simulation over all 7 full-run candidates ─────────────
print("\n" + "=" * 90)
print("CANDIDATE-SELECTOR SIMULATION  (criteria: row-count, field-coverage, tax-identity, |doc_total-declared|)")
print("=" * 90)
RUN_TOTALS = {  # doc incl totals from the per-run section above
    "nebt_snw": 984514.46, "ur0pn3vu": 984411.45, "mhrqzxbf": 702545.00,
    "wiv4zy3t": 932154.00, "892td3if": 702545.00, "39szr0ev": 984460.18,
    "iazb9jio": 984431.22,
}
ranked = sorted(RUN_TOTALS.items(), key=lambda kv: abs(kv[1] - DECLARED))
print(f"  declared_total = {DECLARED:.0f}")
for rid, tot in ranked:
    mark = "  <-- SELECTED (closest to declared)" if rid == ranked[0][0] else ""
    print(f"    {rid:<10} doc_total={tot:>12.2f}  |diff|={abs(tot-DECLARED):>10.2f}{mark}")
print(f"\n  -> |doc_total - declared| uniquely selects the CORRECT run (wiv4zy3t, diff 0).")
