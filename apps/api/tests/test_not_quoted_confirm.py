""""原文明确不报价" 从"字段存在但没有入口"到能真正走通（2026-08-23）。

触发点：手工测试徐汇亨通报价，`HYA-2*0.5` 那一行原表单价/合价都印着"/"（标准
答案的核对说明列写着「PDF原表单价、合价均标为/」）。转换成 CSV/xlsx 之后，
"/"只留在了核对说明列（→ `remark` 字段），单价合价两格本身变成纯空白——
`classify_amount_cell` 只认格子**自身**的取值，看不到这条备注，只能把这行归入
`missing_total_rows` 来问人。

后端一直认 `item.not_quoted` 这个字段（`quote_confirmation_service._gate_missing_total`
附近的 `or bool(item.get("not_quoted"))`），但：
  ① missing_total_rows 没带 remark，人没有证据可看；
  ② 前端从没设置过 `not_quoted`，问了也没有地方能回答。
这份测试锁住修完之后的样子：**备注随行到达，标记能让同一行真正入库**。
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.api.models import BidQuoteLine


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


def _mk_client(monkeypatch, tmp_path, canned: dict):
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider
    from apps.api.intelligence.base import ExtractionResponse

    class _Provider(MockProvider):
        def extract(self, images, schema, prompt, timeout=90, **kwargs):
            return ExtractionResponse(
                data=canned, raw_text=json.dumps(canned, ensure_ascii=False),
                confidence=1.0, tokens_used=0, provider="mock-not-quoted", duration_ms=1,
            )

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        "apps.api.main._build_pipeline", lambda: ExtractionPipeline(_Provider()))
    from apps.api.main import app
    return TestClient(app)


def _upload(c) -> str:
    r = c.post(
        "/api/intake/upload",
        data={"type": "quote", "category": "电缆", "project_id": ""},
        files={"file": ("亨通.png", _png(), "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


# 两行：一行原文写了"/"但只留在备注里（真实场景），一行是真的读不到——
# 用来断言"确认未报价"是**逐行**生效，不是把整份文件都豁免。
_OVERRIDES = [
    {"material": "普通电缆", "spec": "HYA-2*0.5", "unit": "米", "qty": 243.35,
     "remark": "PDF原表单价、合价均标为/"},
    {"material": "普通电缆", "spec": "WDZAN-YJY-4*10+E10", "unit": "米", "qty": 12.33,
     "unit_price": 17.37},   # 这行是真的没读到合价，不该被 not_quoted 免检
]


def _confirm(c, job_id, overrides):
    return c.post("/api/quotes/batch-confirm", json={
        "job_id": job_id, "supplier_name": "亨通", "category": "电缆",
        "overrides": overrides,
    })


def test_missing_total_response_carries_the_remark(monkeypatch, tmp_path, auth_override):
    """人要判断"是不是明确不报价"，前提是能看到原文备注——这条断言的是它
    真的随 `missing_total_requires_review` 的 review_rows 一起到前端，而不是
    像此前那样被后端读进 `item.get("remark")` 却从不写回响应。"""
    with _mk_client(monkeypatch, tmp_path, {"supplier_name": "亨通", "items": []}) as c:
        job_id = _upload(c)
        r = _confirm(c, job_id, _OVERRIDES)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "missing_total_requires_review"
        rows = {row["index"]: row for row in detail["review_rows"]}
        assert rows[0]["remark"] == "PDF原表单价、合价均标为/"
        assert rows[1]["remark"] == ""


def test_not_quoted_flag_lets_that_row_through(monkeypatch, tmp_path, auth_override, db_session):
    """`not_quoted=True` 在第 0 行 → 这一行入库、total_price 为 None、
    不再出现在 missing_total 报错里；第 1 行**没有**这个标记 → 依然拦。"""
    with _mk_client(monkeypatch, tmp_path, {"supplier_name": "亨通", "items": []}) as c:
        job_id = _upload(c)
        confirmed_row_only = [dict(_OVERRIDES[0], not_quoted=True)]
        r = _confirm(c, job_id, confirmed_row_only)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["line_count"] == 1

    lines = db_session.query(BidQuoteLine).filter(
        BidQuoteLine.submission_id == data["submission_id"]).all()
    assert len(lines) == 1
    assert lines[0].total_price is None
    assert lines[0].spec == "HYA-2*0.5"


def test_not_quoted_does_not_blanket_exempt_the_whole_file(monkeypatch, tmp_path, auth_override):
    """标记只对打了标记的那一行生效——同一次提交里没打标记的行必须继续被拦，
    不能因为文件里有一行合法"不报价"就把整份的 missing_total 门关掉。"""
    with _mk_client(monkeypatch, tmp_path, {"supplier_name": "亨通", "items": []}) as c:
        job_id = _upload(c)
        overrides = [dict(_OVERRIDES[0], not_quoted=True), _OVERRIDES[1]]
        r = _confirm(c, job_id, overrides)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "missing_total_requires_review"
        indexes = {row["index"] for row in detail["review_rows"]}
        assert indexes == {1}, "打了 not_quoted 的第 0 行不该再出现在待确认列表里"
