"""miancun_diagnostic_align.py — 绵存一对一内容诊断对齐。

绵存 PDF 无序号列，正式 baseline seq recall = 0%。
本脚本通过名称+规格+数量模糊匹配，为每条 extracted 行找最佳 golden 候选，
输出诊断报告，不修改正式 baseline 数字。

约束：
- 每条 golden 行只能被匹配一次（one-to-one）
- 正式 seq recall 保持 0%（本文件不影响 e2e_diff 输出）
- 输出 matched / conflict / unmatched 及候选分数
- 禁止多行匹配同一 golden 行

用法：
    python scripts/miancun_diagnostic_align.py
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

GOLDEN_PATH = REPO / "data" / "golden" / "quote_miancun.json"
SNAP_PATH = REPO / "tests" / "fixtures" / "ocr_snapshots" / "quote_miancun.json"
PDF_PATH = REPO / "docs" / "test" / "上海绵存投标文件.pdf"
OUT_DIR = REPO / "outputs" / "e2e_diff" / "quote_miancun"


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return "".join(s.split()).lower()


def _name_score(a: str, b: str) -> float:
    a, b = _normalize(a), _normalize(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _qty_score(a, b) -> float:
    try:
        fa, fb = float(a or 0), float(b or 0)
        if fa == 0 and fb == 0:
            return 0.5
        if fa == 0 or fb == 0:
            return 0.0
        return 1.0 if abs(fa - fb) / max(fa, fb) < 0.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _spec_score(a: str, b: str) -> float:
    a, b = _normalize(a), _normalize(b)
    if not a or not b:
        return 0.5  # absent on both sides = neutral
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio() * 0.8


def score_pair(extracted: dict, golden: dict) -> float:
    name = _name_score(extracted.get("name"), golden.get("name"))
    spec = _spec_score(extracted.get("spec"), golden.get("spec"))
    qty = _qty_score(extracted.get("qty"), golden.get("qty"))
    # Weighted: name dominates, spec second, qty confirms
    return name * 0.5 + spec * 0.3 + qty * 0.2


def align(extracted_rows: list[dict], golden_rows: list[dict]) -> dict:
    """Greedy one-to-one matching: best overall score first."""
    pairs: list[tuple[float, int, int]] = []  # (score, ex_idx, g_idx)
    for ei, ex in enumerate(extracted_rows):
        for gi, g in enumerate(golden_rows):
            s = score_pair(ex, g)
            if s > 0.3:  # ignore clearly wrong pairs
                pairs.append((s, ei, gi))
    pairs.sort(key=lambda x: -x[0])

    matched_ex: set[int] = set()
    matched_g: set[int] = set()
    matches: list[dict] = []

    for score, ei, gi in pairs:
        if ei in matched_ex or gi in matched_g:
            continue
        matched_ex.add(ei)
        matched_g.add(gi)
        ex, g = extracted_rows[ei], golden_rows[gi]
        # Field-level comparison for matched pairs
        field_diffs = []
        for f in ("name", "spec", "unit", "qty", "total_price_incl_tax"):
            ev = ex.get(f)
            gv = g.get(f)
            if ev != gv and not (ev is None and gv is None):
                field_diffs.append({"field": f, "extracted": ev, "golden": gv})
        matches.append({
            "golden_seq": g.get("seq"),
            "golden_name": g.get("name"),
            "extracted_name": ex.get("name"),
            "score": round(score, 3),
            "source_page": ex.get("_page"),
            "field_diffs": field_diffs,
        })

    unmatched_g = [g for gi, g in enumerate(golden_rows) if gi not in matched_g]
    unmatched_ex = [ex for ei, ex in enumerate(extracted_rows) if ei not in matched_ex]

    return {
        "matched": matches,
        "unmatched_golden": [{"seq": g.get("seq"), "name": g.get("name")} for g in unmatched_g],
        "unmatched_extracted": [{"name": ex.get("name"), "spec": ex.get("spec")} for ex in unmatched_ex],
        "summary": {
            "golden_rows": len(golden_rows),
            "extracted_rows": len(extracted_rows),
            "matched": len(matches),
            "unmatched_golden": len(unmatched_g),
            "unmatched_extracted": len(unmatched_ex),
            "high_confidence_matches": sum(1 for m in matches if m["score"] >= 0.7),
            "low_confidence_matches": sum(1 for m in matches if m["score"] < 0.7),
        },
    }


def main():
    # Load golden
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    golden_rows = golden["rows"]

    # Replay snapshot to get extracted rows
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    provider = SnapshotProvider(None, SNAP_PATH, mode="replay")
    draft = recognize_tables(str(PDF_PATH), provider, _get_quote_adapter())

    extracted_rows = []
    for r in draft.rows:
        if r.row_type != "quote_line":
            continue
        f = r.fields
        extracted_rows.append({
            "name": f.get("name"),
            "spec": f.get("spec"),
            "unit": f.get("unit"),
            "qty": f.get("qty"),
            "total_price_incl_tax": f.get("total_price_incl_tax") or f.get("total_price"),
            "_page": r.source_ref.page if r.source_ref else None,
        })

    print(f"golden={len(golden_rows)} extracted={len(extracted_rows)}")
    result = align(extracted_rows, golden_rows)

    s = result["summary"]
    print(f"\n=== 绵存诊断对齐 ===")
    print(f"matched: {s['matched']}/{s['golden_rows']} golden, {s['matched']}/{s['extracted_rows']} extracted")
    print(f"  high-confidence (score≥0.7): {s['high_confidence_matches']}")
    print(f"  low-confidence (score<0.7):  {s['low_confidence_matches']}")
    print(f"  unmatched golden:    {s['unmatched_golden']}")
    print(f"  unmatched extracted: {s['unmatched_extracted']}")

    # Field diff summary for matched pairs
    field_err: dict[str, int] = {}
    for m in result["matched"]:
        for fd in m["field_diffs"]:
            field_err[fd["field"]] = field_err.get(fd["field"], 0) + 1
    if field_err:
        print("\n字段差异（仅已匹配行）：")
        for f, cnt in sorted(field_err.items(), key=lambda x: -x[1]):
            pct = cnt / max(s["matched"], 1)
            print(f"  {f}: {cnt}/{s['matched']} = {pct:.0%}")

    # Write outputs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "diagnostic_align.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ 诊断输出: {out.relative_to(REPO)}")

    # Note: This is diagnostic only. Official baseline seq recall = 0%.
    print("\n[注] 本输出仅用于诊断，不修改正式 baseline 指标。")


if __name__ == "__main__":
    main()
