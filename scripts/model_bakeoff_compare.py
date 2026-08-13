"""model_bakeoff_compare.py — 多模型横向对比：行数 / 逐字段准确率 / 顺序保持 / 耗时。

口径与 cable_diff_report 一致（内容对齐 + 合计行当校验和），额外补两项本轮关心的：
  - 保序率：能配对上的行里有多少保持相同先后次序（决定顺序直连能否用）
  - 耗时：来自各次运行的 summary.json

用法：
    python scripts/model_bakeoff_compare.py tmp/vl_bakeoff_v2=qwen3-vl-plus \
        tmp/vl_37plus=qwen3.7-plus tmp/vl_37flash=qwen3.7-flash
"""
from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.cable_diff_report import (            # noqa: E402
    DOCS, align, close, load_golden, load_vl, norm_spec, select_copy, split_rows,
)


def _lis(seq: list[int]) -> int:
    tails: list[int] = []
    for x in seq:
        i = bisect.bisect_right(tails, x)
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(tails)


def score(out_dir: Path, name: str) -> dict:
    slug, declared, basis = DOCS[name]
    got, meta = load_vl(name, basis, out_dir)
    want = load_golden(slug, meta.get("unit_price_basis", basis),
                       meta.get("total_price_basis", basis))
    if not got:
        return {"doc": name, "failed": True, "want": len(want)}
    detail, _sub, totals = split_rows(got, declared)
    # 文件里印了多套清单时只对一套打分（副本不是重复，见 select_copy）
    detail, copies = select_copy(detail)
    a = align(detail, want)
    vi = {id(g): i for i, g in enumerate(detail)}
    gi = {id(w): j for j, w in enumerate(want)}
    order = [gi[id(w)] for _, w in sorted(
        ((vi[id(g)], w) for g, w in a["pairs"]), key=lambda x: x[0])]
    s = sum(v for r in detail if (v := r["total_price"]) is not None)
    fb = a["field_bad"]
    # golden 该字段无权威值时（如某份的含税单价 Excel 未存）不参与评分——
    # 当成"不对"会把一份完全正确的产物报成 0 全对。
    ok_fields = sum(1 for g, w in a["pairs"]
                    if all(w[f] is None or close(g[f], w[f])
                           for f in ("qty", "unit_price", "total_price")))
    return {
        "doc": name, "rows": len(detail), "want": len(want), "matched": a["matched"],
        "missing": len(a["missing"]), "extra": len(a["extra"]),
        "clean_rows": ok_fields, "field_bad": fb,
        "in_order": _lis(order), "sum_delta": round(s - declared, 2), "copies": len(copies),
        "declared": declared, "total_rows_found": len(totals),
    }


def main() -> int:
    runs = []
    for arg in sys.argv[1:]:
        p, _, label = arg.partition("=")
        runs.append((Path(p), label or p))
    if not runs:
        print(__doc__)
        return 2

    secs = {}
    for path, label in runs:
        f = path / "summary.json"
        if f.exists():
            secs[label] = {r["doc"]: r.get("seconds") for r in
                           json.loads(f.read_text(encoding="utf-8"))}

    for name in DOCS:
        print(f"\n=== {name}（参考 {DOCS[name][1]:,.2f}）")
        print(f"  {'模型':16}{'明细':>6}{'应有':>6}{'匹配':>6}{'缺':>5}{'多':>5}"
              f"{'四项全对':>9}{'保序':>6}{'合价求和差':>15}{'耗时s':>8}")
        for path, label in runs:
            r = score(path, name)
            if r.get("failed"):
                print(f"  {label:16}{'识别失败':>10}")
                continue
            t = (secs.get(label) or {}).get(name)
            cp = f" x{r['copies']}套" if r.get('copies') else ""
            print(f"  {label:16}{r['rows']:>6}{r['want']:>6}{r['matched']:>6}"
                  f"{r['missing']:>5}{r['extra']:>5}{r['clean_rows']:>9}"
                  f"{r['in_order']:>6}{r['sum_delta']:>+15,.2f}"
                  f"{(t if t is not None else 0):>8.0f}{cp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
