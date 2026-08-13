"""批量 E2E：对多个项目跑 LLM 填表并输出对比表。

用法：
    python scripts/test_e2e_llm_fill_batch.py --projects 62,63 --category 阀门
    python scripts/test_e2e_llm_fill_batch.py --projects 62 --category 阀门 --assert-regression

每个项目输出一行：
    project_id  quoted_safe_rate  quoted_pending_rate  fp_count  mwe_count  pending  supplier_err  can_finalize

批量通过标准（每项目）：
    quoted_safe_rate ≥ 50%
    quoted_pending_rate ≥ 70%
    false_positive_align_count == 0
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import os

import requests

API = "http://localhost:8002"
DB = os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db")


def log(m: str):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _login(user: str, password: str) -> str:
    return requests.post(
        f"{API}/api/auth/login",
        json={"username": user, "password": password},
        timeout=10,
    ).json()["access_token"]


def _get_project_meta(pid: int, category: str) -> tuple[list[int], int | None]:
    """Return (supplier_ids, session_id) for a project."""
    conn = sqlite3.connect(DB)
    sids = [r[0] for r in conn.execute(
        "SELECT DISTINCT supplier_id FROM quotes WHERE project_id=? AND supplier_id IS NOT NULL",
        (pid,),
    ).fetchall()]
    srow = conn.execute(
        "SELECT id FROM tender_list_sessions WHERE project_id=? AND category=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1",
        (pid, category),
    ).fetchone()
    conn.close()
    return sids, (srow[0] if srow else None)


def _run_project(pid: int, category: str, headers: dict) -> dict:
    sids, session_id = _get_project_meta(pid, category)
    if len(sids) < 2:
        return {"project_id": pid, "error": f"供应商不足2家: {sids}"}
    if not session_id:
        return {"project_id": pid, "error": f"无 TenderListSession (category={category})"}

    r = requests.post(
        f"{API}/api/analysis/tender-list/llm-fill",
        json={"project_id": pid, "category": category, "supplier_ids": sids,
              "tender_list_session_id": session_id, "mode": "replace"},
        headers=headers, timeout=600,
    )
    if r.status_code != 200:
        return {"project_id": pid, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    llm = r.json()
    total = llm.get("anchors_total", 1) or 1
    cmp_any  = llm.get("comparable_2plus", 0)
    cmp_qot  = llm.get("comparable_2plus_quoted", cmp_any)
    fp_cnt   = llm.get("false_positive_align_count", 0)
    rdy      = llm.get("readiness") or {}
    mwe_cnt  = rdy.get("missing_without_evidence_count", 0)
    err_cnt  = rdy.get("supplier_error_count", 0)
    can_fin  = rdy.get("can_finalize", True)
    pending_cnt = sum(f.get("pending", 0) for f in llm.get("per_supplier_fill", []))

    return {
        "project_id": pid,
        "anchors_total": total,
        "quoted_safe_rate": round(cmp_qot / total, 3),
        "quoted_pending_rate": round(cmp_any / total, 3),
        "false_positive_align_count": fp_cnt,
        "missing_without_evidence_count": mwe_cnt,
        "pending_count": pending_cnt,
        "supplier_error_count": err_cnt,
        "can_finalize": can_fin,
        "warnings": rdy.get("warnings", []),
        "error": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", required=True, help="逗号分隔的 project_id 列表，例如 62,63")
    ap.add_argument("--category", default="阀门")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--assert-regression", action="store_true",
                    help="对每个项目断言验收标准，失败则 exit 1")
    args = ap.parse_args()

    pids = [int(p.strip()) for p in args.projects.split(",") if p.strip()]
    log(f"批量 E2E — 项目: {pids}  品类: {args.category}")

    tok = _login(args.user, args.password)
    H = {"Authorization": f"Bearer {tok}"}

    results = []
    for pid in pids:
        log(f"  ► project_id={pid} ...")
        res = _run_project(pid, args.category, H)
        results.append(res)
        if res.get("error"):
            log(f"    ERROR: {res['error']}")
        else:
            log(f"    quoted_safe={res['quoted_safe_rate']*100:.1f}%  "
                f"quoted_pending={res['quoted_pending_rate']*100:.1f}%  "
                f"fp={res['false_positive_align_count']}  "
                f"can_finalize={res['can_finalize']}")

    # ── 汇总表 ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"  {'项目':>6}  {'quoted_safe':>11}  {'q+pending':>9}  "
          f"{'fp':>4}  {'mwe':>4}  {'pend':>5}  {'err':>4}  {'can_fin':>8}  warnings")
    print("  " + "-" * 86)
    all_pass = True
    for r in results:
        if r.get("error"):
            print(f"  {r['project_id']:>6}  ERROR: {r['error']}")
            all_pass = False
            continue
        qs  = f"{r['quoted_safe_rate']*100:.1f}%"
        qp  = f"{r['quoted_pending_rate']*100:.1f}%"
        ok_qs  = r["quoted_safe_rate"]  >= 0.50
        ok_qp  = r["quoted_pending_rate"] >= 0.70
        ok_fp  = r["false_positive_align_count"] == 0
        ok_fin = r["can_finalize"]
        row_ok = ok_qs and ok_qp and ok_fp
        marker = "✓" if row_ok else "✗"
        print(f"  {marker} {r['project_id']:>6}  {qs:>11}  {qp:>9}  "
              f"{r['false_positive_align_count']:>4}  "
              f"{r['missing_without_evidence_count']:>4}  "
              f"{r['pending_count']:>5}  "
              f"{r['supplier_error_count']:>4}  "
              f"{str(ok_fin):>8}  "
              + ("  ".join(r["warnings"]) if r["warnings"] else ""))
        if not row_ok:
            all_pass = False

    print("=" * 90)
    if args.assert_regression:
        if all_pass:
            print("BATCH PASS")
        else:
            print("BATCH FAIL — 见上")
            sys.exit(1)
    else:
        print("BATCH 完成（未启用 --assert-regression 断言）")


if __name__ == "__main__":
    main()
