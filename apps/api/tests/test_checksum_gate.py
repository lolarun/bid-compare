"""声明总价闭环门：提交前阻断，幂等路径不得绕过。

背景（2026-08-09 复核发现）：这道校验原本在 `db.commit()` **之后**执行、阈值 5%、
只写 `job.result` 不阻断。实测方向判错一页造成 0.63%（129,532 元）的偏差会被判 pass
并正常入库——等于没有门。
"""
from __future__ import annotations

import pytest

from apps.api.core.domain_config import CHECKSUM_BLOCK_DELTA_RATIO
from apps.api.services.submission.quote_confirmation_service import _build_checksum


class _Job:
    def __init__(self, declared):
        self.result = {"_doc_meta": {"bid_total": declared}} if declared is not None else {}


def test_exact_match_passes():
    cs = _build_checksum(_Job(20_597_048.33), 20_597_048.33, 136)
    assert cs["status"] == "pass" and cs["delta_pct"] == 0.0


def test_rounding_level_difference_passes():
    """136 行两位小数的累积舍入在 2000 万上不到百万分之一，必须放行。"""
    cs = _build_checksum(_Job(20_597_048.33), 20_597_048.37, 136)
    assert cs["status"] == "pass"


def test_orientation_grade_error_is_blocked():
    """实测方向判错一页 = 129,532 元 = 0.63%。旧的 5% 阈值会放行，必须拦住。"""
    declared = 20_597_048.33
    cs = _build_checksum(_Job(declared), declared - 129_532.01, 136)
    assert cs["status"] == "fail"
    assert cs["delta_pct"] == pytest.approx(0.629, abs=0.01)


def test_threshold_boundary():
    declared = 1_000_000.0
    just_inside = declared * (1 - CHECKSUM_BLOCK_DELTA_RATIO)
    just_outside = declared * (1 - CHECKSUM_BLOCK_DELTA_RATIO * 1.01)
    assert _build_checksum(_Job(declared), just_inside, 10)["status"] == "pass"
    assert _build_checksum(_Job(declared), just_outside, 10)["status"] == "fail"


def test_missing_declared_total_is_unknown_not_pass():
    """文件没给声明总价 = 我们没有这个证据，**不等于校验通过**。"""
    cs = _build_checksum(_Job(None), 12345.0, 10)
    assert cs["status"] == "unknown"
    assert cs["status"] != "pass"
    assert cs["reason"]


def test_zero_or_garbage_declared_total_is_unknown():
    for bad in (0, -1, "", "n/a"):
        assert _build_checksum(_Job(bad), 100.0, 10)["status"] == "unknown"


def test_no_lines_is_unknown():
    assert _build_checksum(_Job(1000.0), 0.0, 0)["status"] == "unknown"


def test_checksum_reports_threshold_for_audit():
    """响应里要带上判据本身，否则事后无法解释为什么拦/不拦。"""
    cs = _build_checksum(_Job(1000.0), 900.0, 10)
    assert cs["threshold_pct"] == pytest.approx(CHECKSUM_BLOCK_DELTA_RATIO * 100)
    assert cs["declared"] == 1000.0 and cs["line_sum"] == 900.0


# ── design/33 §6 决策②：补位金额不进这道门 ──────────────────────────────────
#
# `_build_checksum` 本身不知道"补位"这回事——它只吃一个现成的 `line_total_sum`。
# 排除逻辑在**累加那一步**（`quote_confirmation_service.confirm_batch` 的主循环），
# 这里必须端到端走一次真实的 batch-confirm，不能只测 `_build_checksum`。
#
# 2026-08-23 复核：design/33 文档原本写着这条"已实现、有测试锁住"，两者都不是
# 真的——`line_total_sum` 当时无条件累加每一行。这条测试补齐它，并且是先红后绿
# 验证过的（临时改掉 confirm_batch 里的排除条件，这条测试立刻失败）。

import io
import json

from fastapi.testclient import TestClient
from PIL import Image


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
                confidence=1.0, tokens_used=0, provider="mock-checksum", duration_ms=1,
            )

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )
    monkeypatch.setattr("apps.api.main._build_pipeline", lambda: ExtractionPipeline(_Provider()))
    from apps.api.main import app
    return TestClient(app)


def test_gap_filled_amounts_are_excluded_from_the_declared_total_checksum(
    temp_db, monkeypatch, tmp_path, auth_override, db_session,
):
    """一行读到的、一行补出来的，声明总价按**只读到的那行**核对。

    声明总价 = 两行的真实合计（150）。若补位的 100 被计入，line_sum 会等于
    declared、门禁 pass——这恰恰是设计要防的：门"过了"是因为补位悄悄把账
    填平，不是因为识别本身完整。line_sum 必须只等于未补位那一行的 50。

    `_doc_meta.bid_total` 走 Paddle 封面抽取才会有值，MockProvider 路径不产
    这个键（`pipeline.py` 的注释原话）——这里直接在 DB 里补上，跟"声明总价
    核对"这道门本身要不要被补位绕过是两件事，不该为了凑齐前置条件去伪造一条
    真实不存在的 Paddle 调用链路。
    """
    from apps.api.models import ExtractionJob

    canned = {
        "supplier_name": "宏胜电缆", "quote_date": "2026-08-01",
        "items": [
            {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米",
             "qty": 10, "unit_price": 5, "total_price": 50},
        ],
    }
    with _mk_client(monkeypatch, tmp_path, canned) as c:
        r = c.post("/api/intake/upload", data={"type": "quote", "category": "电缆", "project_id": ""},
                   files={"file": ("宏胜.png", _png(), "image/png")})
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        job = db_session.get(ExtractionJob, job_id)
        job.result = {**(job.result or {}), "_doc_meta": {"bid_total": 150.0}}
        db_session.commit()

        overrides = [
            {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米",
             "qty": 10, "unit_price": 5, "total_price": 50},
            {"material": "YJV-3*70", "spec": "0.6/1kV", "unit": "米",
             "qty": 20, "unit_price": 5, "total_price": 100,
             "validation_flags": ["gap_filled"]},
        ]
        r = c.post("/api/quotes/batch-confirm", json={
            "job_id": job_id, "supplier_name": "宏胜电缆", "category": "电缆",
            "overrides": overrides, "dry_run": True,
        })
        assert r.status_code == 200, r.text
        checksum = next(
            (i for i in r.json()["issues"] if i.get("error") == "declared_total_mismatch"), None)
        # 补位那 100 被排除后，line_sum 只剩 50——跟声明的 150 差了 100，
        # 远超门禁阈值，checksum 门必须命中而不是被补位悄悄填平。
        assert checksum is not None, (
            "补位金额混进了 line_sum——门禁没有命中，声明总价核对被悄悄填平了")
        assert checksum["checksum"]["line_sum"] == pytest.approx(50.0)
