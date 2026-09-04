"""p2_acceptance_run.py — design/26 §6 formal P2 acceptance run (real API, real spend).

跑的是**生产 provider**（`apps.api.intelligence.providers.paddle_ocr.submit_and_parse`），
不是探索脚本 `try_paddleocr_vl.py` 自己的提交/轮询实现——P4 接线用的就是这一条
代码路径，P2 的评测必须跟生产实际执行零漂移（跟 design/24 dry-run 同一个论证）。

每次调用的原始响应立即落盘、绑 code_sha/pdf_sha（design/26 P0 先例，这次用在
生产 provider 的真实产物上）——评分脚本（`e2e_diff.py`）以后如果又发现 bug，
离线重打分不用再花一分钱重新调用。这是用户复核时点名要求的第一优先级。

运行计划（design/26 §6，用户 2026-08-13 批准的估算）：
  1. 耗时基准：7 份文档各跑 1 次，**串行**——干净的耗时数据，这个项目"20-25倍"
     的核心论据依赖它不被并发限流退避污染。
  2. 稳定性：每份文档再跑 2 次，可以并发——只需要输出方差数据，不需要干净耗时。
  合计 21 次调用，约 500 页，成本上限 ¥90（如果账号有企业认证 1000 页/月免费
  额度，很可能是 ¥0——这次真实调用本身就是配额检查，没有另外做一次探针调用）。

用法：
    python scripts/p2_acceptance_run.py                       # 完整计划（21 次调用）
    python scripts/p2_acceptance_run.py --doc kaishuo --runs 1  # 冒烟测试：先跑一份一次
    python scripts/p2_acceptance_run.py --score-only            # 只重打分已缓存的产物，不调用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from e2e_diff import diff_doc  # noqa: E402
from try_paddleocr_vl import (  # noqa: E402 复用同一套文档清单，不重复定义
    DOCS,
    SEVEN_QUOTE_DOCS,
)

from apps.api.intelligence.paddle_vl import build_quote_csv  # noqa: E402
from apps.api.intelligence.providers import paddle_ocr  # noqa: E402
from apps.api.intelligence.vl_quote import build_draft  # noqa: E402

OUT_DIR = REPO / "outputs" / "paddle_p2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:                                              # noqa: BLE001
        return "unknown"


def run_one(doc_key: str, run_idx: int) -> dict:
    """单次调用：提交→轮询→下载，立即落盘原始响应，返回耗时/成功状态。"""
    pdf_path, golden_path = DOCS[doc_key]
    if not pdf_path.exists():
        return {"doc": doc_key, "run": run_idx, "error": f"找不到文件：{pdf_path}"}

    t0 = time.time()
    try:
        result = paddle_ocr.submit_and_parse(str(pdf_path))
    except Exception as exc:                                        # noqa: BLE001
        return {"doc": doc_key, "run": run_idx, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}
    duration = round(time.time() - t0, 1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{doc_key}_run{run_idx}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"doc": doc_key, "run": run_idx, "seconds": duration,
           "pdf_sha256": _sha256(pdf_path), "output_path": str(out_path)}


def score_run(doc_key: str, run_idx: int) -> dict | None:
    """已落盘的某次运行结果 → 走生产适配器 + 修复后的评分脚本，产出行级/字段级指标。"""
    pdf_path, golden_path = DOCS[doc_key]
    out_path = OUT_DIR / f"{doc_key}_run{run_idx}.json"
    if not out_path.exists() or not golden_path or not golden_path.exists():
        return None
    doc_json = json.loads(out_path.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    csv_text = build_quote_csv(doc_json)
    if csv_text is None:
        return {"doc": doc_key, "run": run_idx, "error": "build_quote_csv -> None"}
    draft = build_draft(csv_text, file_path=str(pdf_path), page_count=999,
                        processed_pages=list(range(1, 1000)), parser_mode="paddle_vl")
    all_rows = [r for r in draft.rows if r.row_type == "quote_line"]
    groups: dict[str, list] = {}
    for r in all_rows:
        cn = str(r.fields.get("copy_no") or "1")
        groups.setdefault(cn, []).append(r)
    selected = groups[max(groups, key=lambda k: len(groups[k]))] if groups else []
    scored = diff_doc(doc_key, golden, selected)
    rl = scored["summary"]["row_level"]
    fm = scored["summary"]["field_metrics"]
    total_incl = sum(
        (r.fields.get("total_price_incl_tax") or r.fields.get("total_price") or 0)
        for r in selected if isinstance(r.fields.get("total_price_incl_tax") or r.fields.get("total_price"), (int, float))
    )
    return {
        "doc": doc_key, "run": run_idx, "quality_status": draft.quality.status,
        "extracted_rows": len(all_rows), "selected_rows": len(selected),
        "copy_groups": {k: len(v) for k, v in groups.items()},
        "row_recall": rl["row_recall"], "row_precision": rl["row_precision"],
        "match_mode": rl.get("match_mode"),
        "field_exact_rate": {k: v.get("exact_rate") for k, v in fm.items()},
        "declared_total_sum": round(total_incl, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", choices=list(DOCS))
    ap.add_argument("--runs", type=int, default=3, help="每份文档跑几次（默认 3：1 基准 + 2 稳定性）")
    ap.add_argument("--score-only", action="store_true", help="只重打分已缓存产物，不发起新调用")
    ap.add_argument("--jobs", type=int, default=4, help="稳定性轮次的并发数（基准轮固定串行）")
    a = ap.parse_args()

    doc_keys = [a.doc] if a.doc else SEVEN_QUOTE_DOCS

    if not a.score_only:
        print(f"跑 {len(doc_keys)} 份 × {a.runs} 轮 · provider=paddle_ocr.submit_and_parse（生产代码路径）")

        # 第 1 轮：串行——耗时基准数据不能被并发限流退避污染。
        print("\n=== 第 1 轮（耗时基准，串行）===")
        for k in doc_keys:
            r = run_one(k, 1)
            status = "OK" if "error" not in r else f"失败：{r['error']}"
            print(f"{k:12s} run1 {r.get('seconds', '?'):>8} s  {status}")

        # 第 2+ 轮：可以并发——只要输出方差数据。
        if a.runs > 1:
            print(f"\n=== 第 2-{a.runs} 轮（稳定性，并发 {a.jobs}）===")
            tasks = [(k, run_idx) for k in doc_keys for run_idx in range(2, a.runs + 1)]
            with ThreadPoolExecutor(max_workers=min(a.jobs, len(tasks))) as pool:
                futures = {pool.submit(run_one, k, ri): (k, ri) for k, ri in tasks}
                for fut in as_completed(futures):
                    r = fut.result()
                    status = "OK" if "error" not in r else f"失败：{r['error']}"
                    print(f"{r['doc']:12s} run{r['run']} {r.get('seconds', '?'):>8} s  {status}")

        manifest = {
            "code_sha": _git_sha(), "docs": doc_keys, "runs_per_doc": a.runs,
            "provider": "apps.api.intelligence.providers.paddle_ocr.submit_and_parse",
        }
        (OUT_DIR / "manifest_p2.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    # 打分：不管是不是刚调用完，都从落盘产物重新读，跟生产适配器+评分脚本零漂移。
    print("\n=== 打分（生产适配器 + 修复后的 e2e_diff.py）===")
    scores: list[dict] = []
    for k in doc_keys:
        for run_idx in range(1, a.runs + 1):
            s = score_run(k, run_idx)
            if s:
                scores.append(s)
                fm = s.get("field_exact_rate", {})
                qty_txt = f"{fm['qty']:.1%}" if fm.get("qty") is not None else "-"
                spec_txt = f"{fm['spec']:.1%}" if fm.get("spec") is not None else "-"
                print(f"{k:12s} run{run_idx} recall={s['row_recall']:.1%} "
                     f"precision={s['row_precision']:.1%} qty={qty_txt} spec={spec_txt}")

    (OUT_DIR / "scores_p2.json").write_text(
        json.dumps({"code_sha": _git_sha(), "scores": scores}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"\n产物 → {OUT_DIR}/（各文档各轮 .json + manifest_p2.json + scores_p2.json）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
