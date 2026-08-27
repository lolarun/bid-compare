"""实验 A — OCR 瓶颈定位：PDF 路 vs 真实内容 CSV 路。

目的：把三家**真实 PDF 报价**的内容整理成**等价 CSV**（同名称/规格/价/数量），
两条路都跑 match + llm-fill，对比 ≥2家可比率：
  - CSV 路 ≫ PDF 路  ⇒ 56% 瓶颈在 OCR（确定性解析能突破）
  - 两路持平          ⇒ 瓶颈在判断（已由实验 B 的 LLM 增益回答）

★ 数据前置（必须人工准备，脚本不能凭空生成，否则无法回答 OCR 瓶颈问题）：
  把 tests/fixtures/documents/ 下三家真实 PDF（上海绵存/凯硕新正/泰科龙）的报价明细，
  逐家整理成**单文件单供应商 CSV**，列含：名称、规格型号、材质、单位、数量、含税单价
  （至少一家带"价税合计"行以验 checksum）。放到一个目录，文件名即供应商名。

用法：
  # PDF 路基线（先跑，得到 PDF 的 comparable_2plus）：
  python scripts/test_e2e_anchor.py        # 产出 E2E_v24_test 项目
  python scripts/test_e2e_llm_fill.py --project E2E_v24_test

  # CSV 路（本脚本）：
  python scripts/test_e2e_experiment_a.py --csv-dir docs/test/real_csv --tender <招标清单.xlsx>

需要后端在 8002 + DASHSCOPE_API_KEY。
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

import requests

API = "http://localhost:8002"
DB = os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db")
DEFAULT_TENDER = os.path.join(
    os.path.dirname(__file__), "..", "docs", "test",
    "金桥地铁上盖J9A-03地块（浦发上城科创智谷）研发及商业项目（阀门）招标清单.xlsx",
)
CATEGORY = "阀门"
PROJECT = "ExperimentA_CSV"
PDF_BASELINE_PCT = 55.6  # v2.5 E2E (50/90)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", required=True, help="单供应商 CSV 目录（文件名=供应商名）")
    ap.add_argument("--tender", default=DEFAULT_TENDER)
    args = ap.parse_args()

    csv_files = sorted(glob.glob(os.path.join(args.csv_dir, "*.csv")) +
                       glob.glob(os.path.join(args.csv_dir, "*.xlsx")))
    if len(csv_files) < 2:
        log(f"FAIL: {args.csv_dir} 下不足 2 个 CSV/xlsx。请先准备真实内容 CSV（见脚本头注）")
        sys.exit(1)

    log("=== 实验 A：CSV 路（确定性解析 → LLM 填表） ===")
    log(f"CSV 文件：{[os.path.basename(f) for f in csv_files]}")

    tok = requests.post(f"{API}/api/auth/login",
                        json={"username": "admin", "password": "admin123"}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    # 清理旧项目
    conn = sqlite3.connect(DB)
    for (pid_old,) in conn.execute("SELECT id FROM projects WHERE name=?", (PROJECT,)).fetchall():
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM quotes WHERE project_id=?", (pid_old,))
        conn.execute("DELETE FROM extraction_jobs WHERE json_extract(context,'$.project_id')=?", (pid_old,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid_old,))
    conn.commit(); conn.close()

    pid = requests.post(f"{API}/api/projects",
                        json={"name": PROJECT, "status": "进行中"}, headers=H).json()["id"]
    log(f"project id={pid}")

    # 上传 + batch-confirm 每家 CSV（走与 PDF 相同的 intake → batch-confirm 主流程）
    for path in csv_files:
        sup_name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as f:
            up = requests.post(f"{API}/api/intake/upload",
                               files={"file": (os.path.basename(path), f, "text/csv")},
                               data={"type": "quote", "project_id": str(pid),
                                     "supplier_name": sup_name}, headers=H)
        if up.status_code != 200:
            log(f"FAIL upload {sup_name}: {up.status_code} {up.text[:200]}"); sys.exit(1)
        jid = up.json()["id"]
        # 轮询
        for _ in range(30):
            j = requests.get(f"{API}/api/intake/jobs/{jid}", headers=H).json()
            if j["status"] == "done":
                break
            if j["status"] == "failed":
                log(f"FAIL parse {sup_name}: {j.get('error','')[:200]}"); sys.exit(1)
            time.sleep(1)
        requests.post(f"{API}/api/quotes/batch-confirm",
                      json={"job_id": jid, "project_id": pid, "category": CATEGORY,
                            "supplier_name": sup_name}, headers=H)
        log(f"  {sup_name}: 已解析入库")

    # 确认 tender session
    with open(args.tender, "rb") as f:
        prev = requests.post(f"{API}/api/analysis/tender-list/preview",
                             files={"file": (os.path.basename(args.tender), f,
                                             "application/vnd.ms-excel")}, headers=H, timeout=30).json()
    conf = requests.post(f"{API}/api/analysis/tender-list/confirm",
                         json={"project_id": pid, "category": CATEGORY,
                               "file_name": os.path.basename(args.tender),
                               "anchors_json": prev.get("items", []),
                               "anchors_total": prev.get("total", 0), "confirmed_by": "expA"},
                         headers=H)
    session_id = conf.json().get("id")

    conn = sqlite3.connect(DB)
    sids = [r[0] for r in conn.execute(
        "SELECT DISTINCT supplier_id FROM quotes WHERE project_id=? AND supplier_id IS NOT NULL",
        (pid,)).fetchall()]
    conn.close()

    # match (embedding) + llm-fill
    requests.post(f"{API}/api/analysis/tender-list/match",
                  data={"project_id": str(pid), "category": CATEGORY,
                        "supplier_ids": ",".join(map(str, sids))}, headers=H, timeout=180)
    llm = requests.post(f"{API}/api/analysis/tender-list/llm-fill",
                        json={"project_id": pid, "category": CATEGORY, "supplier_ids": sids,
                              "tender_list_session_id": session_id, "mode": "replace"},
                        headers=H, timeout=600).json()

    anchors_total = llm["anchors_total"]
    csv_cmp2 = llm["comparable_2plus"]
    csv_pct = csv_cmp2 / max(anchors_total, 1) * 100

    print("\n" + "=" * 60)
    print("实验 A 结果：CSV 路 vs PDF 路")
    print("=" * 60)
    print(f"  采购清单锚点:        {anchors_total}")
    print(f"  CSV 路 可比≥2:       {csv_cmp2}/{anchors_total} = {csv_pct:.1f}%")
    print(f"  PDF 路 基线:         {PDF_BASELINE_PCT:.1f}%（v2.5 E2E 50/90，或用 test_e2e_llm_fill 实测）")
    delta = csv_pct - PDF_BASELINE_PCT
    print(f"  Δ (CSV − PDF):       {delta:+.1f}%  "
          f"{'→ 瓶颈在 OCR' if delta >= 5 else '→ 瓶颈在判断（embedding+canonical 天花板）'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
