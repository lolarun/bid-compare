"""强制重新识别（`/intake/jobs/{id}/re-recognize`）。

**为什么需要这个入口**：`create_job` 的幂等键是
`(file_hash, type, context_hash)`——**里面没有代码版本**。识别逻辑改好之后，
同一份文件在同一项目下重传会命中旧 job、拿回改动前的旧结果。这个坑本轮咬过
两次（项目 106 的品类判定，以及三份报价单"明细合计 ¥0"），此前用户唯一的出路
是新建项目换掉 context_hash——既不直观，也留下一堆废项目。

这份测试锁两件事：**默认仍然幂等**（重复上传照样省掉识别调用，那是真实的钱），
以及 **force 确实能绕过幂等**。
"""
from __future__ import annotations

import io

from PIL import Image


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


def _service(db_session):
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider
    from apps.api.services.ingestion.document_ingestion import DocumentIngestionService

    return DocumentIngestionService(db_session, ExtractionPipeline(MockProvider()))


def test_default_is_still_idempotent(db_session, tmp_path, monkeypatch):
    """**默认行为一个字节都不能变。** 识别是要花钱的，重复上传省掉那次调用是
    有意的设计，不是可以顺手放弃的东西。"""
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path)
    svc = _service(db_session)
    content = _png()
    ctx = {"project_id": 1, "category": "阀门"}
    a = svc.create_job(content, "a.png", "quote", ctx)
    b = svc.create_job(content, "a.png", "quote", ctx)
    assert a.id == b.id, "同文件同上下文重传应命中幂等，不该新建 job"


def test_force_bypasses_idempotency(db_session, tmp_path, monkeypatch):
    """`force=True` 必须真的绕过幂等，产出**新** job。

    这是"识别逻辑改了、要拿新结果"唯一的出路；不绕过就等于这个功能不存在。
    """
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path)
    svc = _service(db_session)
    content = _png()
    ctx = {"project_id": 1, "category": "阀门"}
    a = svc.create_job(content, "a.png", "quote", ctx)
    b = svc.create_job(content, "a.png", "quote", ctx, force=True)
    assert a.id != b.id, "force=True 没有绕过幂等"
    assert b.status == "pending", "强制新建的 job 应当是待执行状态"


def test_force_keeps_type_and_context(db_session, tmp_path, monkeypatch):
    """重跑必须沿用原 job 的 type 和 context——供应商归属、项目、品类都不能变，
    否则重新识别会把这份报价挪到别的地方去。"""
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path)
    svc = _service(db_session)
    ctx = {"project_id": 7, "supplier_id": 42, "category": "电缆"}
    a = svc.create_job(_png(), "a.png", "quote", ctx)
    b = svc.create_job(_png(), "a.png", "quote", ctx, force=True)
    assert b.type == a.type
    assert b.context == a.context


def test_old_job_is_left_intact(db_session, tmp_path, monkeypatch):
    """旧 job **不删不改**——留作对照和审计。"""
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path)
    svc = _service(db_session)
    ctx = {"project_id": 1}
    a = svc.create_job(_png(), "a.png", "quote", ctx)
    old_id, old_status = a.id, a.status
    svc.create_job(_png(), "a.png", "quote", ctx, force=True)
    still = svc.get_job(old_id)
    assert still is not None and still.status == old_status
