"""Taikelong layered divergence — 3 non-empty runs (read-only, no API).

Runs: ia6ji99g(60) / 6ljwj5_u(77) / ck4fhg1g(84) seqs.

For each run, reconstruct the pipeline layers from the snapshot:
  L1 visual classification  -> per-page role + orientation
  L2 target_pages (tgt)     = pages in QUOTE_TARGET_ROLES
  L3 no_rot_tgt             = tgt pages with visual orientation==0  (-> _detect_doc_rotation input)
  L4 OCR-HTML quality       = _orientation_quality of each stored quote-page HTML
                              (high q => rotation fired & good; low/0 => not corrected)
  L5 LLM seq coverage       = which golden seqs were extracted

Goal: find the FIRST layer where the runs diverge, and whether the divergence
is driven by the visual classification feeding a different tgt/no_rot_tgt set
into the rotation-fallback vote (Codex's coupling hypothesis).

Golden: quote pages p5..p14, ALL 90°; seqs 1..89.
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

RUNS = [
    ("ia6ji99g", "fresh_snap_ia6ji99g_quote_taikelong.json"),  # 60
    ("6ljwj5_u", "fresh_snap_6ljwj5_u_quote_taikelong.json"),  # 77
    ("ck4fhg1g", "fresh_snap_ck4fhg1g_quote_taikelong.json"),  # 84
]

# rotation constant for reference
from apps.api.intelligence.table_recognizer import _ORIENT_MIN_GOOD  # noqa: E402


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:10]


g = json.loads((ROOT / "data/golden/quote_taikelong.json").read_text("utf-8"))
GOLD = {int(r["seq"]): r for r in g["rows"] if str(r.get("seq")).isdigit()}
pages_g = {p["page"]: p for p in json.loads(
    (ROOT / "data/golden/pages_taikelong.json").read_text("utf-8"))["pages"]}


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
    by_page = {}
    PRIORITY = {"flash": 0, "plus": 1, "review": 1, "review2": 1, "plus_failed": 1}
    for entry in snap.get("visual", {}).values():
        res = entry.get("result")
        cand = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
        for pc in cand:
            if not isinstance(pc, dict):
                continue
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
    page_rot = {p: c["orientation"] for p, c in pages.items() if c.get("orientation")}
    no_rot_tgt = [p for p in tgt if not page_rot.get(p)]

    # OCR inventory: content-hash -> seq coverage + quality
    ocr = []
    for img_hash, html in snap.get("ocr", {}).items():
        ss = seqset_from_html(html)
        ocr.append({
            "chash": _h(html), "len": len(html),
            "q": _orientation_quality(html, 1, "quote"),
            "sig": _orientation_signal(html, "quote"),
            "seqset": ss,
            "seqrange": (min(ss), max(ss)) if ss else None,
        })
    # LLM seq coverage
    allseq = set()
    for v in snap.get("llm", {}).values():
        d = v.get("data") or {}
        for it in (d.get("items", []) if isinstance(d, dict) else []):
            s = str(it.get("seq"))
            if s.isdigit():
                allseq.add(int(s))
    return {"pages": pages, "tgt": tgt, "page_rot": page_rot,
            "no_rot_tgt": no_rot_tgt, "ocr": ocr, "seqs": allseq,
            "n_visual": len(snap.get("visual", {})), "n_ocr": len(snap.get("ocr", {})),
            "n_llm": len(snap.get("llm", {}))}


A = {rid: analyze(f) for rid, f in RUNS}

print("=" * 104)
print(f"TAIKELONG LAYERED DIVERGENCE  (golden quote pages p5-p14 ALL 90°; seq 1-89; _ORIENT_MIN_GOOD={_ORIENT_MIN_GOOD})")
for rid, _ in RUNS:
    print(f"  {rid}: {len(A[rid]['seqs'])} seqs | {A[rid]['n_visual']} vis / {A[rid]['n_ocr']} ocr / {A[rid]['n_llm']} llm")
print("=" * 104)

print("\n### L1 — PER-PAGE VISUAL ROLE/ORIENTATION  (golden | ia6ji99g(60) | 6ljwj5_u(77) | ck4fhg1g(84))")
print(f"  {'pg':<4}{'golden':<26}{'ia6ji99g':<26}{'6ljwj5_u':<26}{'ck4fhg1g':<26}")
for pg in range(1, 17):
    gp = pages_g.get(pg, {})
    cells = [f"{gp.get('role','?')}/{gp.get('orientation','?')}"]
    roles = []
    for rid, _ in RUNS:
        c = A[rid]["pages"].get(pg, {})
        roles.append(f"{c.get('role','-')}/{c.get('orientation','-')}/{c.get('source','-')}" if c else "-")
    cells += roles
    flag = "  <<<" if len(set(roles)) > 1 else ""
    print(f"  p{pg:<3}{cells[0]:<26}{cells[1]:<26}{cells[2]:<26}{cells[3]:<26}{flag}")

print("\n### L2/L3 — TARGET & ROTATION-FALLBACK INPUT SETS")
for rid, _ in RUNS:
    print(f"  [{rid}]  tgt={A[rid]['tgt']}")
    print(f"  {'':<{len(rid)+4}}page_rot(visual)={A[rid]['page_rot']}")
    print(f"  {'':<{len(rid)+4}}no_rot_tgt={A[rid]['no_rot_tgt']}   <- _detect_doc_rotation sees THIS set")

print("\n### L4 — OCR-HTML QUALITY INVENTORY  (q>=MIN_GOOD => rotation fired & table good)")
for rid, _ in RUNS:
    print(f"  --- {rid} ({len(A[rid]['seqs'])} seqs) ---")
    good = sum(1 for o in A[rid]["ocr"] if o["q"] >= _ORIENT_MIN_GOOD)
    low = sum(1 for o in A[rid]["ocr"] if 0 < o["q"] < _ORIENT_MIN_GOOD)
    zero = sum(1 for o in A[rid]["ocr"] if o["q"] == 0)
    print(f"      OCR htmls: q>=MIN_GOOD={good}  0<q<MIN_GOOD={low}  q==0={zero}")
    for o in sorted(A[rid]["ocr"], key=lambda x: (x["seqrange"] or (999, 999))):
        sr = f"{o['seqrange'][0]}-{o['seqrange'][1]}" if o["seqrange"] else "(none)"
        print(f"        chash={o['chash']} len={o['len']:>5} q={o['q']} sig={int(o['sig'])} "
              f"seqs={sr:<8} n={len(o['seqset'])}")

print("\n### L5 — LLM SEQ COVERAGE vs golden (1-89)")
for rid, _ in RUNS:
    s = A[rid]["seqs"]
    missing = [x for x in range(1, 90) if x not in s]
    print(f"  [{rid}] {len(s)}/89   missing={missing}")

print("\n### FIRST-DIVERGENCE SUMMARY")
# Compare tgt sets and no_rot_tgt sets
tgts = {rid: tuple(A[rid]["tgt"]) for rid, _ in RUNS}
nrt = {rid: tuple(A[rid]["no_rot_tgt"]) for rid, _ in RUNS}
print(f"  tgt identical across runs?        {len(set(tgts.values()))==1}   {tgts}")
print(f"  no_rot_tgt identical across runs?  {len(set(nrt.values()))==1}   {nrt}")
