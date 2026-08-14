"""api_e2e_compare.py — 纯 HTTP API 层面的端到端比价验证。

全程只调 REST 接口，不 import 业务函数，因此验证的是真实交付链路：

    创建项目
      -> 招标清单 preview / confirm      (TenderAnchor 行轴)
      -> N 份投标文件 upload + 轮询       (识别)
      -> batch-confirm                   (BidSubmission + BidQuoteLine)
      -> tender-list/match               (锚点对齐)
      -> bid-matrix                      (比价矩阵)

全部参数化：文档、品类、项目名、并发、地址都由命令行给出，脚本内不写死任何
供应商、项目、文件名或页码。

用法：
    python scripts/api_e2e_compare.py \
        --tender  tests/fixtures/documents/tender_list/prj2_附件一_电缆清单.xlsx \
        --bid     tests/fixtures/documents/bid/prj1_上海浦东.pdf \
        --bid     tests/fixtures/documents/bid/prj1_亨通.pdf \
        --category 电缆 \
        --project-name "华泾镇D5B-1电缆"

    加 --dry-run 只做前置检查（文件存在、服务可达、清单可解析），不发起识别。

产物写入 --out 目录（默认 tmp/api_e2e_<时间戳>/）：
    report.json    逐阶段耗时、行数守恒、每份文档的识别结果摘要
    <stage>.json   每个接口的原始响应，便于事后追溯
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent


# ── 小工具 ────────────────────────────────────────────────────────────────────

class Runner:
    """记录每一步的耗时与原始响应。"""

    def __init__(self, base_url: str, out_dir: Path, token: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.token = token
        self.steps: list[dict] = []

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def call(self, stage: str, method: str, path: str, **kw) -> tuple[int, object]:
        t0 = time.time()
        resp = requests.request(
            method, f"{self.base}{path}", headers=self._headers(), timeout=kw.pop("timeout", 600), **kw
        )
        dt = time.time() - t0
        try:
            body = resp.json()
        except Exception:
            body = {"_raw_text": resp.text[:2000]}
        self.steps.append({
            "stage": stage, "method": method, "path": path,
            "status": resp.status_code, "seconds": round(dt, 2),
        })
        self._dump(stage, {"status": resp.status_code, "body": body})
        print(f"  [{resp.status_code}] {method} {path}  {dt:.1f}s  ({stage})")
        return resp.status_code, body

    def _dump(self, name: str, payload: object) -> None:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        (self.out / f"{safe}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def fail(self, msg: str) -> None:
        print(f"\n!! {msg}")
        self.finish(ok=False, error=msg)
        sys.exit(1)

    def finish(self, ok: bool, error: str = "", **extra) -> dict:
        report = {
            "ok": ok,
            "error": error,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "total_seconds": round(sum(s["seconds"] for s in self.steps), 1),
            "steps": self.steps,
            **extra,
        }
        (self.out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return report


def poll_job(run: Runner, job_id: str, label: str, timeout_s: int, interval_s: float) -> dict:
    """轮询识别任务直至 done/failed，返回最终 job。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_s:
        resp = requests.get(
            f"{run.base}/api/intake/jobs/{job_id}", headers=run._headers(), timeout=60
        )
        job = resp.json()
        status, stage = job.get("status"), job.get("stage") or job.get("progress_stage") or ""
        if stage and stage != last:
            print(f"     · {label}: {stage}")
            last = stage
        if status in {"done", "failed"}:
            elapsed = time.time() - t0
            run.steps.append({
                "stage": f"recognize:{label}", "method": "POLL",
                "path": f"/api/intake/jobs/{job_id}", "status": status,
                "seconds": round(elapsed, 2),
            })
            run._dump(f"job_{label}", job)
            print(f"  [{status}] 识别完成 {label}  {elapsed:.1f}s")
            return job
        time.sleep(interval_s)
    run.fail(f"{label}: 识别超时（>{timeout_s}s）")
    return {}


def count_items(job: dict) -> int:
    result = job.get("result") or {}
    items = result.get("items")
    return len(items) if isinstance(items, list) else 0


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--tender", required=True, help="招标清单 Excel（锚点来源）")
    ap.add_argument("--bid", action="append", required=True, help="投标文件 PDF，可重复")
    ap.add_argument("--category", required=True)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--project-code", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--job-timeout", type=int, default=1800, help="单份文档识别超时秒数")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--confirm-force", action="store_true",
                    help="清单中存在 unknown 品类时归入 --category 并写审计标记")
    ap.add_argument("--dry-run", action="store_true", help="只做前置检查，不发起识别")
    args = ap.parse_args()

    tender = Path(args.tender)
    bids = [Path(b) for b in args.bid]
    missing = [str(p) for p in [tender, *bids] if not p.exists()]
    if missing:
        print("以下文件不存在：\n  " + "\n  ".join(missing))
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else REPO / "tmp" / f"api_e2e_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"产物目录: {out}\n")

    run = Runner(args.base_url, out)

    # 0. 服务可达 + 登录
    try:
        requests.get(f"{run.base}/api/projects", timeout=10)
    except Exception as e:
        print(f"服务不可达 {run.base}: {e}")
        return 2
    code, body = run.call("00_login", "POST", "/api/auth/login",
                          json={"username": args.user, "password": args.password})
    if code == 200 and isinstance(body, dict):
        run.token = body.get("access_token", "")

    # 1. 招标清单 preview —— 不落库，先看行数与品类分布
    with tender.open("rb") as fh:
        code, preview = run.call("10_tender_preview", "POST", "/api/analysis/tender-list/preview",
                                 files={"file": (tender.name, fh)})
    if code != 200:
        run.fail(f"清单预览失败: {preview}")
    items = preview.get("items") or []
    breakdown = preview.get("breakdown") or {}
    unknown = preview.get("unknown_count", 0)
    print(f"\n  清单解析: {len(items)} 行 | 品类分布 {breakdown} | unknown {unknown}\n")

    if args.dry_run:
        report = run.finish(ok=True, mode="dry-run",
                            anchors_previewed=len(items), breakdown=breakdown,
                            unknown_count=unknown, bids=[b.name for b in bids])
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    # 2. 建项目（不自动创建，必须显式建）
    code, proj = run.call("20_project", "POST", "/api/projects",
                          json={"name": args.project_name, "code": args.project_code})
    if code not in (200, 201):
        run.fail(f"创建项目失败: {proj}")
    project_id = proj["id"]

    # 3. 清单 confirm —— 生成 TenderListSession（行轴）
    code, confirmed = run.call("30_tender_confirm", "POST", "/api/analysis/tender-list/confirm",
                               json={
                                   "project_id": project_id,
                                   "category": args.category,
                                   "file_name": tender.name,
                                   "anchors_json": items,
                                   "anchors_total": len(items),
                                   "confirmed_by": "api_e2e",
                                   "source_type": "excel",
                                   "force": args.confirm_force,
                               })
    if code != 200:
        run.fail(f"清单确认失败: {confirmed}")
    sessions = confirmed.get("sessions") or []
    anchors_total = sum(s.get("anchors_total", 0) for s in sessions)
    print(f"\n  已确认 {len(sessions)} 个清单会话，锚点合计 {anchors_total}\n")

    # 4. 逐份投标文件识别（串行提交，服务端内部并发）
    submissions: list[dict] = []
    for bid in bids:
        label = bid.stem[-12:]
        print(f"  上传 {bid.name} ({bid.stat().st_size/1e6:.1f}MB)")
        with bid.open("rb") as fh:
            code, job = run.call(f"40_upload_{label}", "POST", "/api/intake/upload",
                                 files={"file": (bid.name, fh)},
                                 data={"type": "quote", "project_id": project_id,
                                       "category": args.category})
        if code != 200:
            run.fail(f"{bid.name} 上传失败: {job}")
        job = poll_job(run, job["id"], label, args.job_timeout, args.poll_interval)
        if job.get("status") != "done":
            print(f"  !! {bid.name} 识别失败，跳过：{job.get('error')}")
            submissions.append({"file": bid.name, "job_id": job.get("id"),
                                "status": "failed", "error": job.get("error")})
            continue

        result = job.get("result") or {}
        supplier_name = (result.get("supplier_name") or "").strip() or bid.stem
        recognized = count_items(job)

        code, conf = run.call(f"50_confirm_{label}", "POST", "/api/quotes/batch-confirm",
                              json={"job_id": job["id"], "supplier_name": supplier_name,
                                    "project_id": project_id, "category": args.category})
        if code != 200:
            run.fail(f"{bid.name} 确认失败: {conf}")
        submissions.append({
            "file": bid.name, "job_id": job["id"], "status": "done",
            "supplier_name": supplier_name,
            "recognized_items": recognized,
            "submission_id": conf.get("submission_id"),
            "confirmed_lines": conf.get("line_count") or conf.get("lines_created"),
        })

    ok_subs = [s for s in submissions if s["status"] == "done" and s.get("submission_id")]
    if not ok_subs:
        run.fail("没有任何投标文件识别并确认成功，无法比价")
    sub_ids = [s["submission_id"] for s in ok_subs]

    # 5. 锚点对齐
    code, matched = run.call("60_match", "POST", "/api/analysis/tender-list/match",
                             data={"project_id": project_id, "category": args.category,
                                   "submission_ids": ",".join(str(i) for i in sub_ids)})
    if code != 200:
        run.fail(f"锚点匹配失败: {matched}")

    # 6. 比价矩阵
    code, matrix = run.call("70_matrix", "POST", "/api/analysis/bid-matrix",
                            json={"project_id": project_id, "category": args.category,
                                  "supplier_ids": [], "submission_ids": sub_ids})
    if code != 200:
        run.fail(f"矩阵生成失败: {matrix}")

    rows = matrix.get("rows") or []
    report = run.finish(
        ok=True,
        project_id=project_id,
        anchors_previewed=len(items),
        anchors_confirmed=anchors_total,
        category_breakdown=breakdown,
        unknown_count=unknown,
        submissions=submissions,
        matrix_rows=len(rows),
        matrix_columns=len(matrix.get("suppliers") or []),
        match_summary={k: matched.get(k) for k in
                       ("matched", "pending", "missing", "total", "per_supplier")
                       if k in matched},
    )

    print("\n" + "=" * 60)
    print(f"锚点(预览/确认)     {len(items)} / {anchors_total}")
    for s in submissions:
        if s["status"] == "done":
            print(f"  {s['file'][:34]:36} 识别 {s['recognized_items']:>4} 行"
                  f" → 确认 {s.get('confirmed_lines')}")
        else:
            print(f"  {s['file'][:34]:36} 识别失败")
    print(f"矩阵                {len(rows)} 行 × {report['matrix_columns']} 列")
    print(f"总耗时              {report['total_seconds']}s")
    print(f"产物                {out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
