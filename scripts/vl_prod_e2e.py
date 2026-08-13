"""vl_prod_e2e.py — 用**生产识别器**跑七份，落盘 draft 并按 golden 打分。

与 vl_direct_bakeoff.py 的区别：那个是脚本自己实现的一条路；这个调的是
`apps/api/intelligence/vl_direct.recognize_quote_vl`，也就是配置
QUOTE_RECOGNIZER=vl_direct 之后生产真正会走的代码。

产物带 manifest（代码 SHA / PDF SHA / 模型 / 提示词哈希 / 投票轮数），
否则事后无法说明这批数字是怎么来的。

用法：
    python scripts/vl_prod_e2e.py --jobs 7 --out tmp/prod_e2e
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apps.api.core.config import get_settings                    # noqa: E402
from apps.api.intelligence.vl_quote import (                    # noqa: E402
    PROMPT_ORIENT, PROMPT_QUOTE_CSV, recognize_quote_vl,
)

DOCS = {
    "上海浦东": "docs/test1/prj1", "亨通": "docs/test1/prj1",
    "宏胜": "docs/test1/prj1", "远东": "docs/test1/prj1",
    "凯硕新正": "docs/test", "上海绵存": "docs/test", "泰科龙": "docs/test",
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _provider():
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    return DashScopeOCRProvider()


def run_one(name: str, out: Path, votes: int) -> dict:
    s = get_settings()
    pdf = next((REPO / DOCS[name]).glob(f"*{name}*.pdf"))
    prov = _provider()
    t0 = time.time()
    try:
        draft = recognize_quote_vl(
            str(pdf),
            vl_call=lambda imgs, prompt: prov.vl_extract_csv(
                imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL),
            orient_call=lambda parts, prompt: prov.vl_extract_csv(
                [b for _t, b in parts], prompt,
                model=s.DASHSCOPE_QUOTE_ORIENT_MODEL,
                labels=[t for t, _b in parts]),
            votes=votes,
        )
    except Exception as exc:                                     # noqa: BLE001
        print(f"[{name}] 失败 {type(exc).__name__}: {exc}", flush=True)
        return {"doc": name, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}

    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"row_index": r.row_index, "row_type": r.row_type,
             "page": r.source_ref.page, "flags": r.validation_flags,
             **{k: r.fields.get(k) for k in
                ("seq", "name", "spec", "unit", "qty", "unit_price", "total_price",
                 "unit_price_excl_tax", "total_price_excl_tax", "not_quoted",
                 "copy_no", "document_row_index", "page_row_index")}}
            for r in draft.rows]
    (d / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    (d / "draft_meta.json").write_text(json.dumps({
        "quality": draft.quality.status,
        "blocking_reasons": draft.quality.blocking_reasons,
        "ledger": draft.ledger.to_dict() if draft.ledger else None,
        "meta": {k: v for k, v in draft.meta.items() if k != "diagnostics"},
        "diagnostics": draft.meta.get("diagnostics"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    rec = {"doc": name, "pdf_sha": _sha(pdf), "pages": draft.page_count,
           "rows": len(draft.rows),
           "quote_lines": sum(1 for r in draft.rows if r.row_type == "quote_line"),
           "quality": draft.quality.status,
           "rotations": draft.meta.get("rotations"),
           "orientation_unresolved": draft.meta.get("orientation_unresolved"),
           "rows_without_page": (draft.meta.get("diagnostics") or {}).get("rows_without_page"),
           "seconds": round(time.time() - t0, 1)}
    print(f"[{name}] → {rec['quote_lines']} 明细 / {rec['rows']} 行 / "
          f"{rec['quality']} / {rec['seconds']}s", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", action="append")
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--out", default="tmp/prod_e2e")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = a.doc or list(DOCS)
    s = get_settings()
    try:
        code_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                           cwd=REPO, text=True).strip()
    except Exception:                                            # noqa: BLE001
        code_sha = "unknown"

    print(f"生产识别器 vl_direct｜抽取 {s.DASHSCOPE_QUOTE_VL_MODEL}｜"
          f"方向 {s.DASHSCOPE_QUOTE_ORIENT_MODEL} × {a.votes} 轮｜并行 {a.jobs}")
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        recs = list(pool.map(lambda n: run_one(n, out, a.votes), names))

    (out / "manifest.json").write_text(json.dumps({
        "code_sha": code_sha,
        "extract_model": s.DASHSCOPE_QUOTE_VL_MODEL,
        "orient_model": s.DASHSCOPE_QUOTE_ORIENT_MODEL,
        "orient_votes": a.votes,
        "prompt_quote_sha": hashlib.sha256(PROMPT_QUOTE_CSV.encode()).hexdigest()[:16],
        "prompt_orient_sha": hashlib.sha256(PROMPT_ORIENT.encode()).hexdigest()[:16],
        "runs": recs,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {out}（含 manifest.json）")
    return 0 if all("error" not in r for r in recs) else 1


if __name__ == "__main__":
    sys.exit(main())
