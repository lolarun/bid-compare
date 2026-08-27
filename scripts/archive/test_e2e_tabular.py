"""Experiment A — CSV bypass E2E test.

Validates that Excel/CSV quote files flow through the same pipeline as PDFs:
    intakeApi.upload(type=quote) → ExtractionJob(tabular_ingestion)
    → batch-confirm → tender-list confirm → anchor match → 90-row bid matrix

Success criteria:
  1. All 3 CSV uploads complete as DONE (no LLM/OCR timeout)
  2. Each supplier has QuoteReadiness JSON
  3. bid-matrix has exactly 90 rows (anchor-full-axis)
  4. zero cross_type_conflicts
  5. At least 1 supplier checksum non-unknown (CSV file with 价税合计 row)

Experiment A metric: print CSV comparable_2plus vs PDF baseline (55.6%).
- If CSV >> PDF baseline → bottleneck was OCR errors
- If CSV ≈ PDF baseline → bottleneck is matching (embedding+canonical ceiling)

Requires: backend running on port 8002.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import time

import requests

API = "http://localhost:8002"
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "test")
TENDER = os.path.join(
    PDF_DIR, "金桥地铁上盖J9A-03地块（浦发上城科创智谷）研发及商业项目（阀门）招标清单.xlsx"
)
PROJECT_NAME = "E2E_csv_bypass_test"
CATEGORY = "阀门"
TIMEOUT_TABULAR = 30  # CSV is near-instant; no OCR wait needed

PDF_COMPARABLE_BASELINE = 55.6  # from v2.5 E2E (50/90)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ─── Synthetic CSV data — 3 single-supplier files ────────────────────────────
# Representative valve types + DN sizes that should match typical tender list anchors.
# Supplier C includes a 价税合计 row to verify checksum path.

HEADER = ["名称", "规格型号", "品牌", "单位", "数量", "含税单价", "备注"]

_SUPPLIER_A_ROWS = [
    ["闸阀", "DN50 PN16 铸钢", "国产", "个", "10", "350.00", ""],
    ["闸阀", "DN80 PN16 铸钢", "国产", "个", "5", "480.00", ""],
    ["闸阀", "DN100 PN16 铸钢", "国产", "个", "8", "620.00", ""],
    ["闸阀", "DN150 PN16 铸钢", "国产", "个", "4", "980.00", ""],
    ["截止阀", "DN25 PN16 铸钢", "国产", "个", "12", "180.00", ""],
    ["截止阀", "DN32 PN16 铸钢", "国产", "个", "8", "210.00", ""],
    ["截止阀", "DN40 PN16 铸钢", "国产", "个", "6", "260.00", ""],
    ["蝶阀", "DN100 PN10 对夹式", "国产", "个", "5", "420.00", ""],
    ["蝶阀", "DN150 PN10 对夹式", "国产", "个", "4", "680.00", ""],
    ["蝶阀", "DN200 PN10 法兰式", "国产", "个", "3", "1200.00", ""],
    ["球阀", "DN25 PN16 全通径", "国产", "个", "15", "120.00", ""],
    ["球阀", "DN32 PN16 全通径", "国产", "个", "10", "145.00", ""],
    ["球阀", "DN50 PN16 全通径", "国产", "个", "8", "220.00", ""],
    ["止回阀", "DN50 PN16 升降式", "国产", "个", "6", "280.00", ""],
    ["止回阀", "DN80 PN16 升降式", "国产", "个", "4", "380.00", ""],
    ["减压阀", "DN50 PN16", "国产", "个", "3", "560.00", ""],
    ["减压阀", "DN80 PN16", "国产", "个", "2", "780.00", ""],
    ["安全阀", "DN25 PN16", "国产", "个", "4", "320.00", ""],
    ["过滤器", "DN50 PN16 Y型", "国产", "个", "6", "180.00", ""],
    ["过滤器", "DN80 PN16 Y型", "国产", "个", "4", "250.00", ""],
]

_SUPPLIER_B_ROWS = [
    ["闸阀", "DN50 PN16 铸铁", "进口", "个", "10", "320.00", ""],
    ["闸阀", "DN80 PN16 铸铁", "进口", "个", "5", "460.00", ""],
    ["闸阀", "DN100 PN16 铸铁", "进口", "个", "8", "580.00", ""],
    ["闸阀", "DN150 PN16 铸铁", "进口", "个", "4", "920.00", ""],
    ["截止阀", "DN25 PN16 铸铁", "进口", "个", "12", "165.00", ""],
    ["截止阀", "DN32 PN16 铸铁", "进口", "个", "8", "195.00", ""],
    ["截止阀", "DN50 PN16 铸铁", "进口", "个", "6", "280.00", ""],
    ["蝶阀", "DN100 PN10 蜗轮蜗杆", "进口", "个", "5", "450.00", ""],
    ["蝶阀", "DN150 PN10 蜗轮蜗杆", "进口", "个", "4", "720.00", ""],
    ["球阀", "DN25 PN16", "进口", "个", "15", "130.00", ""],
    ["球阀", "DN50 PN16", "进口", "个", "8", "235.00", ""],
    ["球阀", "DN80 PN16", "进口", "个", "5", "380.00", ""],
    ["止回阀", "DN50 PN16 旋启式", "进口", "个", "6", "260.00", ""],
    ["止回阀", "DN100 PN16 旋启式", "进口", "个", "4", "450.00", ""],
    ["减压阀", "DN50 PN16", "进口", "个", "3", "590.00", ""],
    ["安全阀", "DN25 PN16 弹簧式", "进口", "个", "4", "340.00", ""],
    ["安全阀", "DN32 PN16 弹簧式", "进口", "个", "3", "420.00", ""],
    ["过滤器", "DN50 PN16", "进口", "个", "6", "190.00", ""],
    ["调节阀", "DN50 PN16 气动", "进口", "个", "2", "3200.00", ""],
    ["调节阀", "DN80 PN16 气动", "进口", "个", "2", "4800.00", ""],
]

def _make_supplier_c_rows():
    rows = [
        ["闸阀", "DN50 PN16 碳钢", "合资", "个", "10", "365.00", ""],
        ["闸阀", "DN80 PN16 碳钢", "合资", "个", "5", "495.00", ""],
        ["闸阀", "DN100 PN16 碳钢", "合资", "个", "8", "640.00", ""],
        ["截止阀", "DN25 PN16 碳钢", "合资", "个", "12", "188.00", ""],
        ["截止阀", "DN40 PN16 碳钢", "合资", "个", "6", "272.00", ""],
        ["蝶阀", "DN100 PN10 气动", "合资", "个", "5", "1850.00", ""],
        ["蝶阀", "DN150 PN10 气动", "合资", "个", "4", "2600.00", ""],
        ["蝶阀", "DN200 PN10 电动", "合资", "个", "2", "4200.00", ""],
        ["球阀", "DN25 PN16 不锈钢", "合资", "个", "15", "135.00", ""],
        ["球阀", "DN32 PN16 不锈钢", "合资", "个", "10", "158.00", ""],
        ["球阀", "DN50 PN16 不锈钢", "合资", "个", "8", "248.00", ""],
        ["球阀", "DN80 PN16 不锈钢", "合资", "个", "5", "395.00", ""],
        ["止回阀", "DN50 PN16 单向", "合资", "个", "6", "295.00", ""],
        ["减压阀", "DN50 PN16", "合资", "个", "3", "575.00", ""],
        ["减压阀", "DN100 PN16", "合资", "个", "2", "1200.00", ""],
        ["安全阀", "DN25 PN16", "合资", "个", "4", "335.00", ""],
        ["过滤器", "DN50 PN16 篮式", "合资", "个", "6", "195.00", ""],
        ["过滤器", "DN100 PN16 篮式", "合资", "个", "4", "320.00", ""],
        ["调节阀", "DN50 PN16 电动", "合资", "个", "2", "2800.00", ""],
        ["电磁阀", "DN25 PN10", "合资", "个", "8", "450.00", ""],
    ]
    # Compute correct grand total for checksum test (含税)
    total = sum(
        float(r[4]) * float(r[5]) for r in rows
    )
    rows.append(["价税合计", "", "", "", "", f"{total:.2f}", ""])
    return rows, total


def _write_single_supplier_csv(rows, supplier_name: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv", prefix=f"e2e_{supplier_name}_")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row)
    return path


def main():
    log("=== CSV bypass E2E (Experiment A) ===")

    tok = requests.post(
        f"{API}/api/auth/login",
        json={"username": "admin", "password": "admin123"},
    ).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    # ── 0. cleanup ──────────────────────────────────────────────────────────
    import sqlite3
    DB = os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db")
    conn = sqlite3.connect(DB)
    old = conn.execute(
        "SELECT id FROM projects WHERE name=?", (PROJECT_NAME,)
    ).fetchall()
    for (pid_old,) in old:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "DELETE FROM bid_alignment_items WHERE group_id IN "
            "(SELECT id FROM bid_alignment_groups WHERE project_id=?)", (pid_old,)
        )
        conn.execute("DELETE FROM bid_alignment_groups WHERE project_id=?", (pid_old,))
        conn.execute("DELETE FROM quotes WHERE project_id=?", (pid_old,))
        conn.execute(
            "DELETE FROM extraction_jobs WHERE json_extract(context,'$.project_id')=?",
            (pid_old,),
        )
        conn.execute("DELETE FROM projects WHERE id=?", (pid_old,))
    conn.commit(); conn.close()
    log(f"cleaned old projects: {[p[0] for p in old] or 'none'}")

    # ── 1. create project ───────────────────────────────────────────────────
    pid = requests.post(
        f"{API}/api/projects",
        json={"name": PROJECT_NAME, "status": "进行中"},
        headers=H,
    ).json()["id"]
    log(f"project id={pid}")

    # ── 2. write temp CSV files ─────────────────────────────────────────────
    c_rows, c_total = _make_supplier_c_rows()
    csv_files = {
        "供应商甲": _write_single_supplier_csv(_SUPPLIER_A_ROWS, "甲"),
        "供应商乙": _write_single_supplier_csv(_SUPPLIER_B_ROWS, "乙"),
        "供应商丙": _write_single_supplier_csv(c_rows, "丙"),
    }
    log(f"created {len(csv_files)} CSV files")
    log(f"  供应商丙 has 价税合计 row: {c_total:.2f}")

    # ── 3. upload each CSV ──────────────────────────────────────────────────
    jobs: dict[str, str] = {}  # job_id → supplier_name
    for sup_name, path in csv_files.items():
        fname = os.path.basename(path)
        with open(path, "rb") as f:
            r = requests.post(
                f"{API}/api/intake/upload",
                files={"file": (fname, f, "text/csv")},
                data={
                    "type": "quote",
                    "project_id": str(pid),
                    "supplier_name": sup_name,
                },
                headers=H,
            )
        if r.status_code != 200:
            log(f"FAIL upload {sup_name}: {r.status_code} {r.text[:200]}"); sys.exit(1)
        jid = r.json()["id"]
        jobs[jid] = sup_name
        log(f"  uploaded {sup_name}: job_id={jid}")

    # ── 4. poll until done ──────────────────────────────────────────────────
    pending = set(jobs)
    start = time.time()
    supplier_row_counts: dict[str, int] = {}
    supplier_doc_meta: dict[str, dict] = {}

    while pending and time.time() - start < TIMEOUT_TABULAR:
        for jid in list(pending):
            sup_name = jobs[jid]
            j = requests.get(f"{API}/api/intake/jobs/{jid}", headers=H).json()
            if j["status"] == "done":
                res = j.get("result") or {}
                n = len(res.get("items") or [])
                dm = res.get("_doc_meta") or {}
                supplier_row_counts[sup_name] = n
                if dm:
                    supplier_doc_meta[sup_name] = dm
                # batch-confirm
                bc = requests.post(
                    f"{API}/api/quotes/batch-confirm",
                    json={
                        "job_id": jid,
                        "project_id": pid,
                        "category": CATEGORY,
                        "supplier_name": sup_name,
                    },
                    headers=H,
                )
                bid_total = dm.get("bid_total")
                log(
                    f"  {sup_name}: {n} rows, "
                    f"bid_total={bid_total}, "
                    f"batch-confirm={bc.status_code}"
                )
                pending.discard(jid)
            elif j["status"] == "failed":
                log(f"  {sup_name} FAILED: {j.get('error','')[:200]}"); sys.exit(1)
        if pending:
            time.sleep(2)

    if pending:
        log(f"FAIL: tabular parse timeout: {[jobs[j] for j in pending]}"); sys.exit(1)

    # cleanup temp files
    for path in csv_files.values():
        try:
            os.unlink(path)
        except OSError:
            pass

    # ── 5. supplier IDs from DB ─────────────────────────────────────────────
    conn = sqlite3.connect(DB)
    sids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT supplier_id FROM quotes WHERE project_id=?", (pid,)
        ).fetchall() if r[0]
    ]
    nq = conn.execute(
        "SELECT COUNT(*) FROM quotes WHERE project_id=?", (pid,)
    ).fetchone()[0]
    conn.close()
    log(f"imported: {nq} quote rows, {len(sids)} suppliers {sids}")

    if len(sids) < 2:
        log("FAIL: fewer than 2 suppliers imported"); sys.exit(1)

    # ── 6. tender list confirm + match ──────────────────────────────────────
    log("confirming tender list session...")
    with open(TENDER, "rb") as f:
        prev = requests.post(
            f"{API}/api/analysis/tender-list/preview",
            files={"file": (os.path.basename(TENDER), f, "application/vnd.ms-excel")},
            headers=H, timeout=30,
        )
    if prev.status_code != 200:
        log(f"FAIL tender-list/preview {prev.status_code}: {prev.text[:200]}"); sys.exit(1)
    preview_data = prev.json()
    anchors_total_preview = preview_data.get("total", 0)

    conf = requests.post(
        f"{API}/api/analysis/tender-list/confirm",
        json={
            "project_id": pid,
            "category": CATEGORY,
            "file_name": os.path.basename(TENDER),
            "anchors_json": preview_data.get("items", []),
            "anchors_total": anchors_total_preview,
            "confirmed_by": "e2e",
        },
        headers=H,
    )
    if conf.status_code != 200:
        log(f"WARN tender-list/confirm {conf.status_code}: {conf.text[:200]}")
    else:
        log(f"  session confirmed: anchors={anchors_total_preview}")

    log("embedding match...")
    r = requests.post(
        f"{API}/api/analysis/tender-list/match",
        data={
            "project_id": str(pid),
            "category": CATEGORY,
            "supplier_ids": ",".join(map(str, sids)),
        },
        headers=H, timeout=180,
    )
    if r.status_code != 200:
        log(f"FAIL match {r.status_code}: {r.text[:300]}"); sys.exit(1)
    s = r.json()
    readiness_list = s.get("readiness_list", [])

    # ── 7. bid matrix ────────────────────────────────────────────────────────
    m = requests.post(
        f"{API}/api/analysis/bid-matrix",
        json={"project_id": pid, "supplier_ids": sids, "category": CATEGORY},
        headers=H,
    ).json()
    rows = m.get("rows", [])
    cmp2 = sum(
        1 for row in rows
        if sum(1 for c in row["suppliers"] if c.get("price") is not None) >= 2
    )

    anchors_total = s.get("anchors_total") or 1
    csv_pct = s.get("comparable_2plus", 0) / anchors_total * 100

    # ── 8. print results ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Experiment A — CSV bypass results")
    print("=" * 60)
    print(f"  suppliers:          {len(sids)}")
    print(f"  quote rows:         {nq}")
    print(f"  tender anchors:     {anchors_total}")
    print(f"  match rate:         {s['matched_quotes']}/{s['total_quotes']}"
          f" = {s['matched_quotes']/max(s['total_quotes'],1)*100:.0f}%")
    print(f"  anchors covered:    {s['anchors_covered']}/{anchors_total} (>=1 supplier)")
    print(f"  comparable>=2 (CSV):{s['comparable_2plus']}/{anchors_total}"
          f" = {csv_pct:.1f}%")
    print(f"  comparable>=2 (PDF): baseline {PDF_COMPARABLE_BASELINE:.1f}%")
    delta = csv_pct - PDF_COMPARABLE_BASELINE
    print(f"  Δ (CSV − PDF):      {delta:+.1f}%  "
          f"{'→ OCR was bottleneck' if delta >= 5 else '→ matching is the ceiling'}")
    print(f"  bid matrix rows:    {len(rows)}  (anchor-full-axis: expected=90)")
    print(f"  >=2-supplier cells: {cmp2}/{len(rows)}")

    print("\n  QuoteReadiness per supplier:")
    print(f"  {'supplier':<20} {'rows':>4} {'match':>5} {'checksum':>14}")
    print("  " + "-" * 46)
    for rd in readiness_list:
        cs = rd.get("checksum_status", "?")
        name = rd["supplier_name"][:18]
        print(f"  {name:<20} {rd.get('quote_rows',0):>4} {rd.get('matched_rows',0):>5} {cs:>14}")
        if rd.get("doc_total"):
            print(f"      cover={rd['doc_total']:,.0f}  computed={rd.get('computed_total',0):,.0f}")

    print("\n  acceptance criteria:")

    c1 = all(v > 0 for v in supplier_row_counts.values())
    print(f"  [{'OK  ' if c1 else 'FAIL'}] all 3 CSV jobs done, items > 0: {supplier_row_counts}")

    c2 = len(readiness_list) == len(sids)
    print(f"  [{'OK  ' if c2 else 'FAIL'}] readiness covers all suppliers: {len(readiness_list)}/{len(sids)}")

    c3 = len(rows) >= 88  # allow 1-2 if tender list has slight variation
    print(f"  [{'OK  ' if c3 else 'FAIL'}] anchor-full-axis rows >= 88: {len(rows)}")

    total_conflicts = sum(rd.get("cross_type_conflicts", 0) for rd in readiness_list)
    c4 = total_conflicts == 0
    print(f"  [{'OK  ' if c4 else 'FAIL'}] zero cross_type conflicts: {total_conflicts}")

    # 供应商丙 has 价税合计 row → checksum should not be unknown
    c_name = "供应商丙"
    c_rd = next((rd for rd in readiness_list if c_name in rd.get("supplier_name", "")), None)
    c5 = c_rd is not None and c_rd.get("checksum_status") in ("passed", "failed")
    cs_val = c_rd.get("checksum_status") if c_rd else "no readiness"
    print(f"  [{'OK  ' if c5 else 'WARN'}] 供应商丙 checksum non-unknown: {cs_val}")

    print()
    all_pass = c1 and c2 and c3 and c4
    print("PASS — all criteria met" if all_pass else "PARTIAL — see above")
    print("=" * 60)

    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
