"""Paddle 报价识别端到端 —— 走真实 HTTP API，不调内部函数（design/26 P4）。

## 为什么必须走 API

脚本直调 `recognize_quote_paddle` 会绕过路由、任务、入库、确认这些真正会出问题
的地方。报价识别现在只有 Paddle 一条路（design/26 P4：不再经过 `LLMProvider`
抽象，`extract_quote` 直接调用 `paddle_ocr.submit_and_parse`），所以只有从
`/api/intake/upload` 进去、并显式验证 `submit_and_parse` 被调用，才能确认
走的是真实生产链路而不是某个默默产出合法 draft 的旁路。

## 确定性从哪来

`PaddleSnapshotReplay` 回放**真实文档的真实 Paddle 识别产物**（design/26 §5/§6
的产物：`outputs/baidu_paddleocr_vl/`（P0）或 `outputs/paddle_p2/`（P2b）下同名
文档的 .json，原样复制进 `tests/fixtures/paddle_snapshots/`），不打 API、无费用、
可入 CI。它测的是模型下游的一切：路由 → 任务 → draft → 确认 → 入库门 → 对齐 →
矩阵 → 导出。模型本身的准确率归 design/26 §6 的 P2 验收矩阵，两者不得互相冒充
（`.claude/rules/tests.md`）。

## 供应商配对：为什么是凯硕+绵存，不是电缆那四份

`docs/test1/prj1` 的四份电缆投标（浦东/亨通/宏胜/远东）实测过 `missing_total`
行数分别是 20/10/0/34（design/26 §6 P2a/P2b）——"原文无合价"门是单行即阻断
（quote_confirmation_service.py 的既定策略，亨通历史上就是这类错位造成过
约 2000 万误差，试点期不接受占比阈值）。四份里只有宏胜是 0，凑不出第二份
干净的搭档。`docs/test` 的凯硕/绵存/泰科龙对同一招标，实测 missing_total 分别
是 0/1/9——凯硕干净，绵存只有 1 行，用 `overrides` 模拟人工修正这一行
（真实工作流本来就是这样：疑点收件箱里改完再点"校对入库"，不是绕过这道门）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.intelligence.base import LLMProvider


class _UnusedTenderOnlyProvider(LLMProvider):
    """占位 provider——本文件全程走 Paddle 报价识别（design/26 P4：quote 不再经过
    `LLMProvider` 抽象），这里只是满足 `ExtractionPipeline.__init__` 的构造要求
    （招标侧仍然用它，本测试从不触发招标识别）。**不能用 `MockProvider`**：
    `extract_quote` 显式识别 `MockProvider` 作为测试替身、直接走它的
    `vl_extract_csv`（服务另外 35 个只关心下游入库/对齐逻辑的集成测试），会绕过
    本文件真正要验证的 Paddle 路径。"""

    name = "unused-tender-only"

    def extract(self, images, schema, prompt, timeout=90):
        raise NotImplementedError  # pragma: no cover — 本测试不会调用招标识别

    def vl_extract_csv(self, images, prompt, *, model=None, labels=None, **kwargs):
        raise NotImplementedError  # pragma: no cover — 本测试不会调用招标识别


REPO = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO / "data" / "golden"
PDF_DIR = REPO / "docs" / "test"

CATEGORY = "阀门"
# (供应商名关键字, 快照 slug, 预期识别行数)。
SUPPLIERS = [
    ("凯硕", "quote_kaishuo", 90),
    ("绵存", "quote_miancun", 87),
]


def _snapshot_available() -> bool:
    return all((REPO / "tests" / "fixtures" / "paddle_snapshots" / f"{slug}.json").exists()
               for _n, slug, _c in SUPPLIERS)


pytestmark = pytest.mark.skipif(
    not _snapshot_available(),
    reason="Paddle 快照缺失；复制 outputs/baidu_paddleocr_vl/<doc>.json 到 "
          "tests/fixtures/paddle_snapshots/quote_<doc>.json",
)


@pytest.fixture
def api(temp_db, tmp_path, monkeypatch):
    """TestClient + Paddle 快照回放 + 免登录。

    provider（tender 侧用，quote 侧不再需要）必须在进入 TestClient **之前**
    换掉：pipeline 是在 lifespan 里由 `_build_pipeline()` 建的，进去之后再替换
    已经晚了。`submit_and_parse` 是模块级函数，直接 monkeypatch 模块属性即可，
    不需要通过 pipeline 注入。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    import apps.api.core.config as config_mod
    config_mod._settings = None                       # 让新 env 生效

    from apps.api.intelligence.paddle_snapshot import PaddleSnapshotReplay
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers import paddle_ocr

    replay = PaddleSnapshotReplay.from_slugs({name: slug for name, slug, _c in SUPPLIERS})
    monkeypatch.setattr(paddle_ocr, "submit_and_parse", replay.submit_and_parse)

    # design/27 §7.1 补的封面 meta 抽取（text_call）如果不在这里挡住，会在测试
    # 环境配了真实 DASHSCOPE_API_KEY 时悄悄打一次真实网络调用——这份测试的
    # 名字和一直以来的定位都是"不打 API、零费用、可入 CI"（回放测试，不是
    # fresh E2E，两者不得互相冒充，`.claude/rules/tests.md`）。返回全空跟
    # "未配置抽取客户端"时的降级行为等价，只是这里额外走一遍新增的抽取代码
    # 路径本身（确认它不报错），不引入真实数据去跟本测试固定的 golden 走查。
    import apps.api.intelligence.paddle_doc_meta as paddle_doc_meta_mod
    monkeypatch.setattr(paddle_doc_meta_mod, "get_text_client_call",
                        lambda: (lambda prompt: ""))

    # **不能用 `MockProvider`**：`extract_quote` 显式识别 `MockProvider` 作为
    # 测试替身、直接走它的 canned CSV（服务另外 35 个只关心下游入库/对齐逻辑
    # 的集成测试），会绕过这里真正要验证的 Paddle 路径。provider 只服务招标侧，
    # 这里的测试从不触发招标识别，用什么占位都行，只是不能是 MockProvider。
    monkeypatch.setattr("apps.api.main._build_pipeline",
                        lambda: ExtractionPipeline(provider=_UnusedTenderOnlyProvider()))

    from apps.api.core.security import get_current_user
    from apps.api.main import app

    app.dependency_overrides[get_current_user] = lambda: {"sub": "e2e", "role": "管理员"}
    with TestClient(app) as client:
        yield client, replay
    app.dependency_overrides.clear()
    config_mod._settings = None


def _anchors_from_golden(slug: str) -> list[dict]:
    """采购清单锚点用 golden 的行。

    招标侧还没有 Paddle 适配器（design/26 §9：`vl_tender.py` 只在 Paddle 覆盖
    了扫描招标件之后才退场，目前条件不成立），所以本测试直接提供锚点而不是
    识别招标 PDF——如实反映当前的覆盖边界：Paddle 目前只覆盖报价侧。
    """
    g = json.loads((GOLDEN_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    return [{"seq": r["seq"], "name": r.get("material") or r.get("name") or "",
             "spec": r.get("spec") or "", "unit": r.get("unit") or "",
             "qty": r.get("qty"), "category": CATEGORY}
            for r in g["rows"]]


def _resolve_review_rows(items: list[dict]) -> list[dict]:
    """模拟"人工在疑点收件箱里补全原文无合价的行"——数量和单价都读到了、只是
    合价没被单独抽出来的行，补 qty×单价当合价；抽取本身就有缺口（数量或单价
    有一项没读到）的行不硬凑，原样保留让它继续走人工核对，不能拿一个猜的数字
    冒充"已确认"。"""
    fixed = []
    for it in items:
        has_total = any(it.get(k) is not None
                        for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax"))
        if has_total or it.get("not_quoted"):
            fixed.append(it)
            continue
        qty, price = it.get("qty"), it.get("unit_price")
        if qty is not None and price is not None:
            it = dict(it, total_price=round(qty * price, 2))
        fixed.append(it)
    return fixed


def test_paddle_full_flow_through_http_api(api):
    client, replay = api

    # ── 1. 项目与供应商 ────────────────────────────────────────────────────
    r = client.post("/api/projects", json={"name": "Paddle-E2E 阀门项目"})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    supplier_ids = {}
    for name, _slug, _c in SUPPLIERS:
        r = client.post("/api/suppliers", json={"name": f"{name}阀门有限公司"})
        assert r.status_code == 201, r.text
        supplier_ids[name] = r.json()["id"]

    # ── 2. 采购清单锚点（行轴）─────────────────────────────────────────────
    anchors = _anchors_from_golden(SUPPLIERS[0][1])
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": project_id, "category": CATEGORY,
        "file_name": "paddle-e2e.xlsx", "anchors_json": anchors,
        "anchors_total": len(anchors), "source_type": "excel",
    })
    assert r.status_code == 200, r.text

    # ── 3-4. 每家：上传 → 任务完成 → 确认入库 ───────────────────────────────
    submission_ids = []
    for name, slug, expected_items in SUPPLIERS:
        replay.current = name                           # 指定本次回放哪一家
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
        assert len(items) == expected_items, f"{name} 应识别 {expected_items} 条，实得 {len(items)}"

        r = client.post("/api/quotes/batch-confirm", json={
            "job_id": job["id"], "supplier_id": supplier_ids[name],
            "supplier_name": f"{name}阀门有限公司",
            "project_id": project_id, "category": CATEGORY,
            "overrides": _resolve_review_rows(items),
        })
        assert r.status_code == 200, f"{name} 入库失败：{r.text}"
        body = r.json()
        assert body["status"] in ("ok", "confirmed"), body
        submission_ids.append(body["submission_id"])

    assert len(set(submission_ids)) == 2, "两家必须是两个独立 submission"

    # ── 5. 对齐 ────────────────────────────────────────────────────────────
    r = client.post("/api/analysis/tender-list/match", data={
        "project_id": str(project_id), "category": CATEGORY,
        "supplier_ids": ",".join(str(supplier_ids[n]) for n, _s, _c in SUPPLIERS),
        "submission_ids": ",".join(str(i) for i in submission_ids),
    })
    assert r.status_code == 200, r.text

    # ── 6. 比价矩阵 ────────────────────────────────────────────────────────
    r = client.post("/api/analysis/bid-matrix", json={
        "project_id": project_id, "category": CATEGORY,
        "supplier_ids": [supplier_ids[n] for n, _s, _c in SUPPLIERS],
        "submission_ids": submission_ids,
    })
    assert r.status_code == 200, r.text
    matrix = r.json()
    assert matrix.get("rows"), "矩阵没有行"

    # ── 7. 导出 ────────────────────────────────────────────────────────────
    r = client.get("/api/export/bid-matrix", params={
        "project_id": project_id, "category": CATEGORY,
        "supplier_ids": ",".join(str(supplier_ids[n]) for n, _s, _c in SUPPLIERS),
    })
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK", "导出的不是 xlsx"


def test_upload_actually_took_the_paddle_branch(api):
    """**没有这条，上面那条测试可能全程在测某个默默产出合法 draft 的旁路而无人
    知晓。** 必须直接验证：`paddle_ocr.submit_and_parse` 确实被调用了，且产出带
    Paddle 专属的行位证据。
    """
    client, replay = api
    name, _slug, _c = SUPPLIERS[0]
    replay.current = name
    calls_before = len(replay.calls)

    r = client.post("/api/projects", json={"name": "Paddle 分支验证"})
    project_id = r.json()["id"]
    pdf = next(PDF_DIR.glob(f"*{name}*.pdf"))
    r = client.post("/api/intake/upload",
                    files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
                    data={"type": "quote", "project_id": str(project_id),
                          "category": CATEGORY})
    assert r.status_code in (200, 201), r.text
    job_id = r.json()["id"]

    calls = replay.calls[calls_before:]
    assert calls, "submit_and_parse 未被调用（说明报价识别没有真的走到 Paddle 这条路）"

    job = client.get(f"/api/intake/jobs/{job_id}").json()   # 见上：上传响应 status 不可信
    items = (job.get("result") or {}).get("items") or []
    assert items, f"没有产出行；job={job.get('status')} error={job.get('error')}"
    # 这三个字段只有 Paddle 路径会填（qwen VL-direct 已删除，legacy 更早删除）。
    first = items[0]
    assert first.get("source_page"), "缺来源页"
    assert first.get("document_row_index") == 1, "缺文档内行序"
    assert "copy_no" in first, "缺副本编号"
