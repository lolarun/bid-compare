"""design/24 B0 —— copy_no 下游去重回归测试。

VL-direct 提示词要求重复副本（正本/副本）照实全部输出并标 copy_no。此前
下游（结构完整性门/checksum）从不消费这个字段，把一份文档的两份合法副本
误判成"重复行占比 50%"甚至 BLOCKED——浦东 272=136×2 正是这个缺陷的实例。

两层测试：
  A. `_dedupe_copies` 纯函数单测（无需 DB/HTTP，覆盖选取规则）。
  B. HTTP 集成：证明修复前会 422 的场景（重复副本触发结构完整性 BLOCKED）
     现在能顺利入库，且响应带着 copy_dedup 证据。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from apps.api.services.submission.quote_confirmation_service import _dedupe_copies


# ── A. 纯函数单测 ────────────────────────────────────────────────────────────

def _row(seq: str, total: float, copy_no: str = "") -> dict:
    return {"material": f"物料{seq}", "spec": "规格", "total_price": total, "copy_no": copy_no}


def test_no_copy_no_passthrough():
    """没有 copy_no（或只有一个空值分组）→ 原样返回，report=None。"""
    items = [_row("1", 100), _row("2", 200)]
    out, report = _dedupe_copies(items, declared_total=300)
    assert out == items
    assert report is None


def test_single_copy_no_value_passthrough():
    """所有行 copy_no 相同（只有一份，没有重复）→ 原样返回。"""
    items = [_row("1", 100, "1"), _row("2", 200, "1")]
    out, report = _dedupe_copies(items, declared_total=300)
    assert out == items
    assert report is None


def test_declared_total_known_picks_closest_sum():
    """声明总价已知 → 选合价之和最接近声明总价的那组，不是选第一份/最大份。"""
    copy1 = [_row("1", 100, "1"), _row("2", 200, "1")]      # sum=300，|300-304|=4
    copy2 = [_row("1", 100, "2"), _row("2", 205, "2")]      # sum=305，|305-304|=1，更接近
    items = copy1 + copy2
    out, report = _dedupe_copies(items, declared_total=304)
    assert out == copy2
    assert report["selected_copy_no"] == "2"
    assert report["selection_basis"] == "closest_to_declared_total"
    assert report["total_copies"] == 2
    assert report["selected_rows"] == 2
    assert report["dropped_rows"] == 2
    assert report["dropped_by_copy"] == {"1": 2}


def test_declared_total_unknown_picks_largest_group():
    """声明总价未知（None/0）→ 退回行数最多的一组，不是任意选一份。"""
    copy1 = [_row("1", 100, "1"), _row("2", 200, "1"), _row("3", 50, "1")]   # 3 行
    copy2 = [_row("1", 100, "2")]                                            # 1 行（残缺副本）
    items = copy1 + copy2
    out, report = _dedupe_copies(items, declared_total=None)
    assert out == copy1
    assert report["selected_copy_no"] == "1"
    assert report["selection_basis"] == "largest_row_count"
    assert report["dropped_rows"] == 1


def test_declared_total_zero_treated_as_unknown():
    """declared_total=0 是"没有这个证据"，不是"目标是 0"——同样退回按行数选。"""
    copy1 = [_row("1", 100, "1")]
    copy2 = [_row("1", 100, "2"), _row("2", 50, "2")]
    items = copy1 + copy2
    out, report = _dedupe_copies(items, declared_total=0)
    assert out == copy2
    assert report["selection_basis"] == "largest_row_count"


def test_three_copies():
    """三份以上副本同样只选一份，其余全部计入 dropped。"""
    items = (
        [_row("1", 100, "1")]
        + [_row("1", 100, "2")]
        + [_row("1", 100, "3")]
    )
    out, report = _dedupe_copies(items, declared_total=100)
    assert len(out) == 1
    assert report["total_copies"] == 3
    assert report["dropped_rows"] == 2
    assert len(report["dropped_by_copy"]) == 2


def test_missing_copy_no_field_entirely():
    """items 完全没有 copy_no 键（老数据/非 VL 来源）→ 视为全部同组，原样返回。"""
    items = [{"material": "A", "total_price": 100}, {"material": "B", "total_price": 200}]
    out, report = _dedupe_copies(items, declared_total=300)
    assert out == items
    assert report is None


# ── B. HTTP 集成：修复前会 BLOCKED 的场景，现在正常入库 ─────────────────────

DUPLICATED_QUOTE = {
    "supplier_name": "远东电缆",
    "quote_date": "2026-08-01",
    "items": [
        {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
         "unit_price": 50, "total_price": 5000, "copy_no": "1"},
        {"material": "YJV-3*70", "spec": "0.6/1kV", "unit": "米", "qty": 200,
         "unit_price": 40, "total_price": 8000, "copy_no": "1"},
        {"material": "YJV-3*50", "spec": "0.6/1kV", "unit": "米", "qty": 300,
         "unit_price": 30, "total_price": 9000, "copy_no": "1"},
        # 副本 2：内容与副本 1 逐行相同（正本/副本重复，非识别错误）
        {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
         "unit_price": 50, "total_price": 5000, "copy_no": "2"},
        {"material": "YJV-3*70", "spec": "0.6/1kV", "unit": "米", "qty": 200,
         "unit_price": 40, "total_price": 8000, "copy_no": "2"},
        {"material": "YJV-3*50", "spec": "0.6/1kV", "unit": "米", "qty": 300,
         "unit_price": 30, "total_price": 9000, "copy_no": "2"},
    ],
}


@pytest.fixture
def copy_dedup_client(temp_db, monkeypatch, tmp_path, auth_override):
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider
    from apps.api.intelligence.base import ExtractionResponse

    class _DupProvider(MockProvider):
        def extract(self, images, schema, prompt, timeout=90, **kwargs):
            return ExtractionResponse(
                data=DUPLICATED_QUOTE,
                raw_text=json.dumps(DUPLICATED_QUOTE, ensure_ascii=False),
                confidence=1.0, tokens_used=0, provider="mock-dup", duration_ms=1,
            )

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )
    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(_DupProvider()),
    )
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


class TestCopyDedupIntegration:
    def test_duplicated_copies_confirm_without_blocking(self, copy_dedup_client):
        """修复前：6 行里 3 行"重复"、金额占比 50% > 10% 阈值 → 422 BLOCKED。
        修复后：先按 copy_no 选一份（3 行），结构完整性门看到的是单份、零重复。
        """
        c = copy_dedup_client
        r = c.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "电缆", "project_id": ""},
            files={"file": ("远东.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        r = c.get(f"/api/intake/jobs/{job_id}")
        assert r.json()["status"] == "done"

        r = c.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_id,
                "supplier_name": "远东电缆",
                "category": "电缆",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()

        # 只入了一份副本，不是 6 行
        assert data["line_count"] == 3, data

        # copy_dedup 证据齐全，前端据此提示用户
        dedup = data["copy_dedup"]
        assert dedup is not None
        assert dedup["total_copies"] == 2
        assert dedup["selected_rows"] == 3
        assert dedup["dropped_rows"] == 3
        assert sorted(dedup["copy_nos"]) == ["1", "2"]

        # 结构完整性门没有把这次入库标成"重复行"问题
        assert data["integrity"]["duplicate_rows"] == 0

    def test_single_copy_document_unaffected(self, copy_dedup_client, monkeypatch):
        """没有重复副本的正常文档：行为与改动前完全一致，copy_dedup=None。"""
        from apps.api.intelligence.pipeline import ExtractionPipeline
        from apps.api.intelligence.providers.mock import MockProvider
        from apps.api.intelligence.base import ExtractionResponse

        single = {
            "supplier_name": "亨通电缆",
            "quote_date": "2026-08-01",
            "items": [
                {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
                 "unit_price": 50, "total_price": 5000, "copy_no": "1"},
            ],
        }

        class _SingleProvider(MockProvider):
            def extract(self, images, schema, prompt, timeout=90, **kwargs):
                return ExtractionResponse(
                    data=single, raw_text=json.dumps(single, ensure_ascii=False),
                    confidence=1.0, tokens_used=0, provider="mock-single", duration_ms=1,
                )

        monkeypatch.setattr(
            "apps.api.main._build_pipeline",
            lambda: ExtractionPipeline(_SingleProvider()),
        )
        from apps.api.main import app

        with TestClient(app) as c:
            r = c.post(
                "/api/intake/upload",
                data={"type": "quote", "category": "电缆", "project_id": ""},
                files={"file": ("亨通.png", _png(), "image/png")},
            )
            job_id = r.json()["id"]
            r = c.post(
                "/api/quotes/batch-confirm",
                json={"job_id": job_id, "supplier_name": "亨通电缆", "category": "电缆"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["line_count"] == 1
            assert data["copy_dedup"] is None
