"""Corrected offline attribution for taikelong (read-only, no API calls).

Compares the successful vs failed fresh snapshots PER PAGE:
  - visual role + orientation (reconstructed from cached visual results)
  - whether the page enters target_pages (QUOTE_TARGET_ROLES)
  - page_rotations / no_rot_tgt (inputs to document-level _detect_doc_rotation)
  - full-page OCR HTML content hash / length / table+row counts / _orientation_quality
  - OCR-html -> seq coverage (via golden tokens)
  - LLM output seq ranges

Golden truth (data/golden/pages_taikelong.json): quote pages = p5..p14, ALL 90°.

Specifically tests the hypothesis: did page 14's classification change the
DOCUMENT rotation candidate (a doc-level vote over no_rot_tgt), thereby changing
p5..p14 orientation/OCR — as opposed to "an extra page moved tile boundaries".

NOTE on reconstructable vs not:
  - probe votes inside _detect_doc_rotation require OCR of *rotated images*, which
    are NOT in the snapshot — so the vote tally itself cannot be recomputed offline.
  - BUT the decisive evidence IS reconstructable: if p5..p14 OCR-HTML content hashes
    are identical across the two runs, their rotation/OCR did NOT change, refuting
    the hypothesis; if they differ, it is supported.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.intelligence.table_recognizer import _orientation_quality, _orientation_signal  # noqa: E402
from apps.api.intelligence.page_classifier import QUOTE_TARGET_ROLES, META_ROLES, VisualPageRole  # noqa: E402

TARGET_ROLE_VALUES = {r.value for r in QUOTE_TARGET_ROLES}
META_ROLE_VALUES = {r.value for r in META_ROLES} | {VisualPageRole.COVER.value}

SUCCESS = "fresh_snap_ck4fhg1g_quote_taikelong.json"
FAIL = "fresh_snap_6ljwj5_u_quote_taikelong.json"


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


# golden tokens for seq coverage detection
g = json.loads((ROOT / "data/golden/quote_taikelong.json").read_text("utf-8"))
GOLD = {}
for r in g["rows"]:
    if str(r.get("seq")).isdigit():
        GOLD[int(r["seq"])] = r
DECLARED = g["declared_total"]
pages_g = {p["page"]: p for p in json.loads((ROOT / "data/golden/pages_taikelong.json").read_text("utf-8"))["pages"]}


def seqset_from_html(html: str) -> set[int]:
    found = set()
    for seq, row in GOLD.items():
        for fld in ("total_price_incl_tax", "unit_price_excl_tax", "total_price_excl_tax"):
            v = row.get(fld)
            if v and ("%g" % v) in html:
                found.add(seq)
                break
    return found


def resolve_pages(snap):
    """Aggregate visual results into a final per-page {role, orientation, conf, source}.
    Pipeline order: flash batch first, then plus-review overrides for reviewed pages.
    We approximate: for each page keep the highest-priority entry
    (review/plus > flash; tie-break by confidence)."""
    by_page = {}
    PRIORITY = {"flash": 0, "plus": 1, "review": 1, "review2": 1}
    for entry in snap.get("visual", {}).values():
        res = entry.get("result")
        if not isinstance(res, list):
            continue
        for pc in res:
            pg = pc.get("page")
            if pg is None:
                continue
            src = pc.get("source", "flash")
            pr = (PRIORITY.get(src, 0), pc.get("confidence") or 0)
            if pg not in by_page or pr > by_page[pg][0]:
                by_page[pg] = (pr, {"role": pc.get("role"),
                                    "orientation": pc.get("orientation"),
                                    "conf": pc.get("confidence"), "source": src})
    return {pg: v[1] for pg, v in by_page.items()}


def analyze(fname):
    snap = json.loads((ROOT / "outputs" / fname).read_text("utf-8"))
    pages = resolve_pages(snap)
    tgt = sorted(p for p, c in pages.items() if c["role"] in TARGET_ROLE_VALUES)
    meta_extra = sorted(p for p, c in pages.items() if c["role"] in META_ROLE_VALUES)
    page_rot = {p: c["orientation"] for p, c in pages.items() if c.get("orientation")}
    no_rot_tgt = [p for p in tgt if not page_rot.get(p)]

    # OCR htmls -> seq coverage + hash + quality
    ocr = []
    for img_hash, html in snap.get("ocr", {}).items():
        ss = seqset_from_html(html)
        ocr.append({
            "img": img_hash[:10], "chash": _h(html), "len": len(html),
            "tables": html.count("<table"), "trs": html.count("<tr"),
            "q": _orientation_quality(html, 1, "quote"),
            "sig": _orientation_signal(html, "quote"),
            "seqset": ss,
            "seqrange": (min(ss), max(ss)) if ss else None,
        })
    # LLM seq ranges
    llm = []
    for v in snap.get("llm", {}).values():
        data = v.get("data") or {}
        items = data.get("items", []) if isinstance(data, dict) else []
        seqs = sorted(int(i["seq"]) for i in items if str(i.get("seq")).isdigit())
        llm.append({"rows": len(items), "seqs": seqs,
                    "range": (min(seqs), max(seqs)) if seqs else None})
    return {"pages": pages, "tgt": tgt, "meta_extra": meta_extra,
            "page_rot": page_rot, "no_rot_tgt": no_rot_tgt, "ocr": ocr, "llm": llm,
            "n_visual": len(snap.get("visual", {})), "n_ocr": len(snap.get("ocr", {})),
            "n_llm": len(snap.get("llm", {}))}


S = analyze(SUCCESS)
F = analyze(FAIL)

print("=" * 100)
print("TAIKELONG DIVERGENCE — CORRECTED ATTRIBUTION   (golden: quote pages p5-p14, ALL 90°)")
print(f"  SUCCESS={SUCCESS}  ({S['n_ocr']} ocr / {S['n_llm']} llm / {S['n_visual']} visual)")
print(f"  FAIL   ={FAIL}  ({F['n_ocr']} ocr / {F['n_llm']} llm / {F['n_visual']} visual)")
print("=" * 100)

print("\n### 1. PER-PAGE VISUAL ROLE + ORIENTATION (p1..p16)  [vs golden]")
print(f"  {'pg':<4}{'golden role/orient':<34}{'SUCCESS role/orient/src':<40}{'FAIL role/orient/src':<40}")
for pg in range(1, 17):
    gp = pages_g.get(pg, {})
    grole = f"{gp.get('role','?')}/{gp.get('orientation','?')}"
    s = S["pages"].get(pg, {})
    f = F["pages"].get(pg, {})
    srole = f"{s.get('role','-')}/{s.get('orientation','-')}/{s.get('source','-')}" if s else "-"
    frole = f"{f.get('role','-')}/{f.get('orientation','-')}/{f.get('source','-')}" if f else "-"
    flag = "  <<<" if srole != frole else ""
    print(f"  p{pg:<3}{grole:<34}{srole:<40}{frole:<40}{flag}")

print("\n### 2. TARGET / ROTATION SETS")
print(f"  SUCCESS tgt        = {S['tgt']}")
print(f"  FAIL    tgt        = {F['tgt']}")
print(f"  SUCCESS page_rot   = {S['page_rot']}")
print(f"  FAIL    page_rot   = {F['page_rot']}")
print(f"  SUCCESS no_rot_tgt = {S['no_rot_tgt']}   (-> _detect_doc_rotation input)")
print(f"  FAIL    no_rot_tgt = {F['no_rot_tgt']}   (-> _detect_doc_rotation input)")

print("\n### 3. OCR-HTML INVENTORY (sorted by seq coverage)  [q=_orientation_quality]")
for tag, A in (("SUCCESS", S), ("FAIL", F)):
    print(f"  --- {tag} ---")
    for o in sorted(A["ocr"], key=lambda x: (x["seqrange"] or (999, 999))):
        sr = f"{o['seqrange'][0]}-{o['seqrange'][1]}" if o["seqrange"] else "(none)"
        print(f"    chash={o['chash']} len={o['len']:>5} tbl={o['tables']} tr={o['trs']:>3} "
              f"q={o['q']} sig={int(o['sig'])} seqs={sr:<8} n={len(o['seqset'])}")

print("\n### 4. DECISIVE — OCR-HTML content hashes by seq-range, SUCCESS vs FAIL")
print("  (identical hash for same seq-range => that page's OCR/rotation did NOT change)")


def by_range(ocr):
    m = {}
    for o in ocr:
        if o["seqrange"]:
            m.setdefault(o["seqrange"], []).append(o["chash"])
    return m


sr_s, sr_f = by_range(S["ocr"]), by_range(F["ocr"])
all_ranges = sorted(set(sr_s) | set(sr_f))
for r in all_ranges:
    hs = set(sr_s.get(r, []))
    hf = set(sr_f.get(r, []))
    if hs and hf:
        verdict = "IDENTICAL" if hs == hf else "DIFFERENT"
    else:
        verdict = "only-SUCCESS" if hs else "only-FAIL"
    print(f"    seq{r[0]:>2}-{r[1]:<2}  SUCCESS={sorted(hs) or '-'}  FAIL={sorted(hf) or '-'}  -> {verdict}")

print("\n### 5. LLM OUTPUT SEQ RANGES")
for tag, A in (("SUCCESS", S), ("FAIL", F)):
    rs = sorted([l["range"] for l in A["llm"] if l["range"]])
    allseq = sorted({s for l in A["llm"] for s in l["seqs"]})
    missing = [s for s in range(1, 90) if s not in allseq]
    extra = [s for s in allseq if s > 89]
    print(f"  {tag}: ranges={rs}")
    print(f"          unique seqs={len(allseq)} missing(1-89)={missing} hallucinated(>89)={extra}")
