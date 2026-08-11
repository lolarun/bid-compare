"""ab_uncertainty.py — 提示词的单变量 A/B 台架（--arms 选变量）。

## 为什么要做

2026-08-10 上海浦东"修复"后：格式崩溃消失、金额从 −6.04% 变成 −0.012%。
但**同一轮里方向判定也变了**（旋转页 3 → 13），两个变量同时动，无法归因。

第一轮（--arms rule5）的结论：**uncertainty 列没有可测贡献**。无此列的对照臂
同样没有格式崩溃，两臂缺格行都是 0/0/0、金额差都是 0/−52/0。真因是方向——
模型当时在读侧躺和倒置的页面，"出声思考"是症状不是病因。

## 怎么消掉方向这个变量

不调方向模型，直接喂一张固定旋转表：`orient_call` 是注入点，返回什么就是什么。
detect_rotations 每轮拿到同一答案 → 全票通过 → 旋转确定。副作用是不花方向的
API 钱，也不受方向不稳的影响。

## 为什么每臂要跑多次

这个模型在同配置下不是确定性的（实测同份文档两次跑出的行数、页码、金额都不同）。
单次对单次比不出东西——那测的是运行间方差，不是提示词。

用法：
    python scripts/ab_uncertainty.py --arms rule5 --doc 上海浦东 --runs 3
    python scripts/ab_uncertainty.py --arms lang  --doc 上海浦东 --runs 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apps.api.core.config import get_settings                     # noqa: E402
from apps.api.intelligence import vl_direct as vd                 # noqa: E402
from apps.api.services.ingestion.draft_integrity import find_duplicate_rows  # noqa: E402

DOCS = {
    "上海浦东": ("docs/test1/prj1", 20629762.68),
    "亨通": ("docs/test1/prj1", 20966959.43),
    "宏胜": ("docs/test1/prj1", 20597048.33),
    "远东": ("docs/test1/prj1", 20014715.08),
    "凯硕新正": ("docs/test", 932154.0),
    "上海绵存": ("docs/test", 1667051.0),
    "泰科龙": ("docs/test", 1067616.41),
}

# 固定旋转表。取自 2026-08-10 那次判定（该次 copy2 金额差 0.012%，是已知最好的一次）。
# A/B 只要求它**恒定**，不要求它绝对正确——两臂用同一张表，方向就不再是变量。
ROTATIONS = {
    "上海浦东": {3: 90, 4: 90, 5: 90, 6: 90, 7: 90,
                 8: 270, 9: 270, 10: 270, 11: 270, 12: 270, 13: 270, 14: 270, 15: 270},
    # 凯硕的表头把含税/不含税分了列（单价(元)不含税 / 合计(元)不含税 / 税率 /
    # 税额(元) / 价税合计(元)）——语言这一轮的关键样本：英文提示词若让模型翻译
    # 表头，税基信息会静默消失，而含税/不含税选错的偏差恰好等于税率。
    "凯硕新正": {9: 90, 10: 90},
}

# 对照臂：加入第 5 条之前的提示词，逐字保留（含当时 3/4 条的"倒数第二列/最后一列"措辞）。
PROMPT_WITHOUT_RULE5 = """请将这份投标文件中的报价清单导出为 CSV 格式给我。只返回 CSV，不要其他说明。

另外遵守四条规则：
1. 小计/合计/总计行要保留，不要跳过。第一列固定为 row_type，标注每行类型：
   明细行填 detail，小计行填 subtotal，总计/合计行填 total。
2. 只转录文档上确实写着的数字。任何单元格为空或看不清就留空，
   不要用数量×单价补算合价，也不要补算任何其他数字。
   原文明确写"不报价"的（如 / 、无、N/A），照原样填这个符号，不要留空。
3. 如果同一份清单在文件里重复出现（例如正本与副本、汇总与明细），照实全部输出，
   不要合并也不要丢弃。倒数第二列固定为 copy_no，标注该行属于第几份（1、2……）。
4. 最后一列固定为 page，填该行来自第几页（按我给你的图像顺序，从 1 开始）。"""

# 英文版：**逐条对应中文版，内容不增不减**。这一轮的变量只有语言——
# 顺手改措辞就会重蹈上一轮的覆辙（两个变量同时动，结果无法归因）。
PROMPT_EN = """Export the quotation list from this bid document as CSV. Return only CSV, no commentary.

Five rules:
1. Keep subtotal and grand-total rows — do not skip them. The first column is row_type:
   detail for line items, subtotal for subtotals, total for grand totals.
2. Transcribe only numbers actually printed in the document. Leave a cell empty when it is
   blank or unreadable. Never compute a line total from quantity x unit price, and never
   compute any other number.
   When the document explicitly marks an item as not quoted (e.g. /, 无, N/A), copy that
   symbol as-is instead of leaving the cell empty.
3. If the same list appears more than once in the file (original and duplicate copy, or
   summary and detail), output every occurrence — do not merge and do not drop any.
   The third-from-last column is copy_no: which occurrence the row belongs to (1, 2, ...).
4. The second-from-last column is page: which page the row came from, counting the images
   I gave you from 1.
5. The last column is uncertainty. When you cannot read a row confidently, put the doubt in
   that row's uncertainty cell; leave it empty otherwise. Do not use commas in this column
   (it breaks the CSV).
   Doubts go only in this column — not in other columns, not on a line of their own, and
   never by switching to a different delimiter."""

ARM_SETS = {
    # 变量：第 5 条（uncertainty 列）。2026-08-10 结论——无可测差异。
    "rule5": {"A_无疑问列": PROMPT_WITHOUT_RULE5, "B_有疑问列": vd.PROMPT_QUOTE_CSV},
    # 变量：提示词语言。两版逐条对应，只有语言不同。
    "lang": {"A_中文": vd.PROMPT_QUOTE_CSV, "B_英文": PROMPT_EN},
}


def _provider():
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    return DashScopeOCRProvider()


def fixed_orient(doc: str):
    """假的方向调用：忽略图像，直接返回固定表。方向由此变成常量。"""
    table = ROTATIONS.get(doc, {})

    def call(_parts, _prompt) -> str:
        return "\n".join(f"{p},{deg}" for p, deg in sorted(table.items()))
    return call


def run_once(doc: str, arm: str, prompt: str, idx: int, out: Path) -> dict:
    cfg = get_settings()
    folder, declared = DOCS[doc]
    pdf = next((REPO / folder).glob(f"*{doc}*.pdf"))
    prov = _provider()
    t0 = time.time()

    # 提示词在 vl_call 这个注入点上替换——它本来就是按参数传进来的，忽略即可。
    # **不要 monkeypatch vd.PROMPT_QUOTE_CSV**：两臂并行时那是同一个模块全局，
    # 会互相覆盖，测出来的东西就不是提示词了。生产入口也不该为做实验长出新口子。
    try:
        draft = vd.recognize_quote_vl(
            str(pdf),
            vl_call=lambda imgs, _p: prov.vl_extract_csv(
                imgs, prompt, model=cfg.DASHSCOPE_QUOTE_VL_MODEL),
            orient_call=fixed_orient(doc),
            votes=1,                      # 固定表，多轮投票没有意义
        )
    except Exception as exc:                                       # noqa: BLE001
        print(f"[{arm} #{idx}] 失败 {type(exc).__name__}: {exc}", flush=True)
        return {"arm": arm, "run": idx, "error": f"{type(exc).__name__}: {exc}"}

    det = [r for r in draft.rows if r.row_type == "quote_line"]
    align = (draft.meta.get("diagnostics") or {}).get("alignment") or {}

    # 副本按 copy_no 切；取**与声明总价最接近**的那一套评分。
    # 不按页区间切：模型自报的页码在运行间会整体偏移（实测 p1-14 vs p2-15）。
    copies: dict[str, list] = {}
    for r in det:
        copies.setdefault((r.fields.get("copy_no") or "").strip(), []).append(r)
    best = None
    for key, rows in copies.items():
        s = sum(r.fields.get("total_price") or 0 for r in rows)
        cand = {"copy": key, "rows": len(rows), "sum": s, "delta": s - declared}
        if best is None or abs(cand["delta"]) < abs(best["delta"]):
            best = cand
    dup = find_duplicate_rows([
        {"material": r.fields.get("name"), "spec": r.fields.get("spec"),
         "qty": r.fields.get("qty"), "unit_price": r.fields.get("unit_price"),
         "total_price": r.fields.get("total_price")}
        for r in det if (r.fields.get("copy_no") or "").strip() == best["copy"]
    ]).to_dict()

    rec = {
        "arm": arm, "run": idx, "seconds": round(time.time() - t0, 1),
        "detail_rows": len(det), "copies": len(copies),
        "extra_cells": align.get("extra_cell_rows"),
        "missing_cells": align.get("missing_cell_rows"),
        "align_verdict": align.get("verdict"),
        "uncertain_rows": sum(1 for r in draft.rows
                              if "model_uncertain" in (r.validation_flags or [])),
        "best_copy": best["copy"], "best_rows": best["rows"],
        "best_sum": round(best["sum"], 2), "delta": round(best["delta"], 2),
        "delta_pct": round(abs(best["delta"]) / declared * 100, 4),
        "dup_groups": len(dup.get("groups", [])),
        "pages_reported": sorted({r.source_ref.page for r in det if r.source_ref.page}),
        # 表头语言必须单独观测：中文提示词下模型也曾自发输出英文表头（远东那次
        # 把打分打成 0 匹配）。语言这一轮尤其要看它稳不稳。
        "header": (draft.meta.get("csv_header") or []),
        "header_is_ascii": all(ord(c) < 128 for c in "".join(draft.meta.get("csv_header") or [])),
    }
    (out / f"{arm}_{idx}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{arm} #{idx}] 明细{rec['detail_rows']} 多格{rec['extra_cells']} "
          f"缺格{rec['missing_cells']} {rec['align_verdict']} "
          f"差{rec['delta']:+,.0f}({rec['delta_pct']}%) 疑问{rec['uncertain_rows']} "
          f"{rec['seconds']}s", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="上海浦东")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--arms", default="rule5", choices=sorted(ARM_SETS))
    ap.add_argument("--out", default="tmp/ab_unc")
    a = ap.parse_args()
    arms = ARM_SETS[a.arms]

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = get_settings()
    print(f"单变量 A/B [{a.arms}]｜{a.doc}｜模型 {cfg.DASHSCOPE_QUOTE_VL_MODEL}｜"
          f"方向固定 {len(ROTATIONS.get(a.doc, {}))} 页｜每臂 {a.runs} 次｜并行 {a.jobs}")

    jobs = [(arm, prompt, i + 1) for arm, prompt in arms.items() for i in range(a.runs)]
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        recs = list(ex.map(lambda j: run_once(a.doc, j[0], j[1], j[2], out), jobs))

    print(f"\n{'臂':<12}{'次数':>5}{'明细行':>16}{'缺格行':>14}{'|差|%':>18}{'疑问行':>10}")
    summary = {}
    for arm in arms:
        rs = [r for r in recs if r.get("arm") == arm and "error" not in r]
        if not rs:
            print(f"{arm:<12}{'全部失败':>5}")
            continue

        def rng(key):
            v = [r[key] for r in rs if r.get(key) is not None]
            if not v:
                return "—"
            return (f"{statistics.median(v):g}" if len(set(v)) == 1
                    else f"{min(v):g}–{max(v):g} (中位{statistics.median(v):g})")
        summary[arm] = rs
        print(f"{arm:<12}{len(rs):>5}{rng('detail_rows'):>16}{rng('missing_cells'):>14}"
              f"{rng('delta_pct'):>18}{rng('uncertain_rows'):>10}")

    (out / "summary.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
