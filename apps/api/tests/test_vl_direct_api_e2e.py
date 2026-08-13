"""VL-direct 端到端 —— 走真实 HTTP API，不调内部函数。

## 为什么必须走 API

脚本直调 `recognize_quote_vl` 会绕过路由、任务、入库、确认这些真正会出问题的地方。
`extract_quote` 现在按 `hasattr(provider, "vl_extract_csv")` 选路：有则 VL，
没有则退到逐页批量兜底——**兜底同样产出合法 draft，断言照样能过**。所以只有从
`/api/intake/upload` 进去、并显式验证 VL 方法被调用，才能确认走的是 VL。

## 确定性从哪来

`VLSnapshotProvider` 回放**真实文档的真实模型输出**（已录制的 CSV + 那次的旋转表），
不打 API、无费用、可入 CI。它测的是模型下游的一切：路由 → 任务 → draft → 确认 →
入库门 → 对齐 → 矩阵 → 导出。模型本身的准确率归 C 层
（`test_cable_accuracy_e2e.py`），两者不得互相冒充（`.claude/rules/tests.md`）。

## 覆盖的链路

    建项目/供应商 → 确认采购清单锚点 → 上传报价 PDF → 轮询任务 → 取 draft
    → batch-confirm 入库 → tender-list/match 对齐 → bid-matrix 比价 → 导出 xlsx
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO / "data" / "golden"
PDF_DIR = REPO / "docs" / "test1" / "prj1"

CATEGORY = "电缆"
# 两家足以覆盖"多 submission 身份 / 矩阵多列"，又不必渲染四份 PDF。
SUPPLIERS = [
    ("宏胜", "quote_cable_hongsheng"),
    ("远东", "quote_cable_yuandong"),
]


def _snapshot_available() -> bool:
    return all((REPO / "tests" / "fixtures" / "vl_snapshots" / f"{slug}.json").exists()
               for _n, slug in SUPPLIERS)


pytestmark = pytest.mark.skipif(
    not _snapshot_available(),
    reason="VL 快照缺失；录制：python scripts/record_vl_snapshots.py --all",
)


@pytest.fixture
def api(temp_db, tmp_path, monkeypatch):
    """TestClient + VL 快照 provider + 免登录。

    provider 必须在进入 TestClient **之前**换掉：pipeline 是在 lifespan 里由
    `_build_pipeline()` 建的，进去之后再替换已经晚了。
    """
    # 不再需要设 QUOTE_RECOGNIZER —— 报价只有 VL 一条路（legacy 报价分支已归档）。
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    import apps.api.core.config as config_mod
    config_mod._settings = None                       # 让新 env 生效

    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.vl_snapshot_provider import VLSnapshotProvider

    providers = {name: VLSnapshotProvider.from_slug(slug) for name, slug in SUPPLIERS}

    class _Router:
        """按上传的文件名挑对应供应商的快照。

        真实 provider 每次调用只看到图像，认不出是谁；测试里必须能区分，否则两家
        会拿到同一份报价，矩阵就成了自己跟自己比。
        """
        def __init__(self):
            self.current: VLSnapshotProvider | None = None
            self.seen: list[str] = []

        def vl_extract_csv(self, images, prompt, **kw):
            assert self.current is not None, "未指定当前供应商"
            return self.current.vl_extract_csv(images, prompt, **kw)

    router = _Router()
    monkeypatch.setattr("apps.api.main._build_pipeline",
                        lambda: ExtractionPipeline(provider=router))

    from apps.api.core.security import get_current_user
    from apps.api.main import app

    app.dependency_overrides[get_current_user] = lambda: {"sub": "e2e", "role": "管理员"}
    with TestClient(app) as client:
        yield client, router, providers
    app.dependency_overrides.clear()
    config_mod._settings = None


def _anchors_from_golden(slug: str) -> list[dict]:
    """采购清单锚点用 golden 的 136 行。

    招标侧**没有 VL 分支**（`extract_tender_bidlist` 硬性要求 legacy 的 provider
    接口），所以本测试直接提供锚点而不是识别招标 PDF。这不是偷懒——它如实反映了
    当前的覆盖边界：VL 只覆盖报价侧。见 docs/design/21 §1。
    """
    g = json.loads((GOLDEN_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    return [{"seq": r["seq"], "name": r.get("material") or r.get("name") or "",
             "spec": r.get("spec") or "", "unit": r.get("unit") or "",
             "qty": r.get("qty"), "category": CATEGORY}
            for r in g["rows"]]


def test_vl_direct_full_flow_through_http_api(api):
    client, router, providers = api

    # ── 1. 项目与供应商 ────────────────────────────────────────────────────
    r = client.post("/api/projects", json={"name": "VL-E2E 电缆项目"})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    supplier_ids = {}
    for name, _slug in SUPPLIERS:
        r = client.post("/api/suppliers", json={"name": f"{name}电缆有限公司"})
        assert r.status_code == 201, r.text
        supplier_ids[name] = r.json()["id"]

    # ── 2. 采购清单锚点（行轴）─────────────────────────────────────────────
    anchors = _anchors_from_golden(SUPPLIERS[0][1])
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": project_id, "category": CATEGORY,
        "file_name": "vl-e2e.xlsx", "anchors_json": anchors,
        "anchors_total": len(anchors), "source_type": "excel",
    })
    assert r.status_code == 200, r.text
    assert len(anchors) == 136

    # ── 3-4. 每家：上传 → 任务完成 → 确认入库 ───────────────────────────────
    submission_ids = []
    for name, slug in SUPPLIERS:
        router.current = providers[name]                       # 指定本次回放哪一家
        pdf = next(PDF_DIR.glob(f"*{name}*.pdf"))

        r = client.post("/api/intake/upload",
                        files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
                        data={"type": "quote", "project_id": str(project_id),
                              "supplier_id": str(supplier_ids[name]),
                              "category": CATEGORY})
        assert r.status_code in (200, 201), r.text
        job = r.json()

        # **上传响应的 status 不可信**：`intake.py` 在 submit_extraction 之后仍返回
        # 调用前捕获的 ORM 对象，而 inline 执行是在另一个 session 里更新行的，
        # 所以这里永远看到 pending。必须重新 GET —— 真实前端也是靠轮询拿状态。
        r = client.get(f"/api/intake/jobs/{job['id']}")
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["status"] == "done", job          # inline 模式下一次 GET 即终态
        items = (job.get("result") or {}).get("items") or []
        assert len(items) == 136, f"{name} 应识别 136 条，实得 {len(items)}"

        r = client.post("/api/quotes/batch-confirm", json={
            "job_id": job["id"], "supplier_id": supplier_ids[name],
            "supplier_name": f"{name}电缆有限公司",
            "project_id": project_id, "category": CATEGORY,
        })
        assert r.status_code == 200, f"{name} 入库失败：{r.text}"
        body = r.json()
        assert body["status"] in ("ok", "confirmed"), body
        submission_ids.append(body["submission_id"])

    assert len(set(submission_ids)) == 2, "两家必须是两个独立 submission"

    # ── 5. 对齐 ────────────────────────────────────────────────────────────
    r = client.post("/api/analysis/tender-list/match", data={
        "project_id": str(project_id), "category": CATEGORY,
        "supplier_ids": ",".join(str(supplier_ids[n]) for n, _ in SUPPLIERS),
        "submission_ids": ",".join(str(i) for i in submission_ids),
    })
    assert r.status_code == 200, r.text

    # ── 6. 比价矩阵 ────────────────────────────────────────────────────────
    r = client.post("/api/analysis/bid-matrix", json={
        "project_id": project_id, "category": CATEGORY,
        "supplier_ids": [supplier_ids[n] for n, _ in SUPPLIERS],
        "submission_ids": submission_ids,
    })
    assert r.status_code == 200, r.text
    matrix = r.json()
    assert matrix.get("rows"), "矩阵没有行"

    # ── 7. 导出 ────────────────────────────────────────────────────────────
    r = client.get("/api/export/bid-matrix", params={
        "project_id": project_id, "category": CATEGORY,
        "supplier_ids": ",".join(str(supplier_ids[n]) for n, _ in SUPPLIERS),
    })
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK", "导出的不是 xlsx"


def test_upload_actually_took_the_vl_branch(api):
    """**没有这条，上面那条测试可能全程在测 legacy 而无人知晓。**

    分支是靠 `hasattr(provider, "vl_extract_csv")` 选的，落回 legacy 时结果仍是
    合法 draft，断言照样能过。所以必须直接验证：provider 的 VL 方法确实被调用了，
    且产出带 VL 专属的行位证据。
    """
    client, router, providers = api
    name, _slug = SUPPLIERS[0]
    router.current = providers[name]
    calls_before = len(providers[name].calls)

    r = client.post("/api/projects", json={"name": "VL 分支验证"})
    project_id = r.json()["id"]
    pdf = next(PDF_DIR.glob(f"*{name}*.pdf"))
    r = client.post("/api/intake/upload",
                    files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
                    data={"type": "quote", "project_id": str(project_id),
                          "category": CATEGORY})
    assert r.status_code in (200, 201), r.text
    job_id = r.json()["id"]

    calls = providers[name].calls[calls_before:]
    assert "extract" in calls, f"VL 抽取未被调用，调用序列={calls}"
    assert calls.count("orient") >= 1, f"方向预检未被调用，调用序列={calls}"

    job = client.get(f"/api/intake/jobs/{job_id}").json()   # 见上：上传响应 status 不可信
    items = (job.get("result") or {}).get("items") or []
    assert items, f"没有产出行；job={job.get('status')} error={job.get('error')}"
    # 这三个字段只有 VL 路径会填；legacy 不产出 document_row_index / copy_no
    first = items[0]
    assert first.get("source_page"), "缺来源页"
    assert first.get("document_row_index") == 1, "缺文档内行序"
    assert "copy_no" in first, "缺副本编号 —— 这是 VL 专属字段"
