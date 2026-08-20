"""design/28 cut 5——POST /api/intake/classify-tier0 集成测试。

真实语料直接过 HTTP 层，不 mock 分类逻辑本身（那部分已经在
test_document_classify.py 里用单测锁死了）；这里验证的是路由层的
落盘/清理/schema 序列化，用真实 xlsx/pdf 走一遍完整请求-响应循环。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "tests" / "fixtures" / "documents"


@pytest.fixture
def client(auth_override):
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"夹具缺失：{path}")


def test_classify_definitive_tender_list(client):
    path = DOCS / "tender_list/prj2_附件一_电缆清单.xlsx"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/vnd.ms-excel")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "excel"
    assert body["verdict"] == "tender_list"
    assert body["confidence"] == "definitive"
    assert body["price_columns"] == []
    assert body["fill_rate"] is None


def test_classify_strong_bid_list(client):
    path = DOCS / "bid_list/凯硕新正投标清单.xlsx"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/vnd.ms-excel")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "bid_list"
    assert body["confidence"] == "strong"
    assert body["fill_rate"] >= 0.9


def test_classify_ambiguous_excel_returns_uncertain_not_error(client):
    path = DOCS / "tender_list/金桥地体上盖招标文件.xlsx"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/vnd.ms-excel")})
    assert r.status_code == 200, r.text  # 不确定是合法答案，不是接口异常
    body = r.json()
    assert body["verdict"] == "uncertain"
    assert body["confidence"] == "ambiguous"


def test_classify_native_pdf(client):
    """design/29 §3 Tier 1.5 接入后：原生招标 PDF 应该判出真实 verdict
    （tender），不再是笼统的 "document"。"""
    path = DOCS / "tender/金桥地体上盖招标文件.pdf"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf"
    assert body["verdict"] == "tender"
    assert body["text_layer"] == "native"


def test_classify_scanned_pdf_without_vision_client_returns_uncertain(client, monkeypatch):
    """2026-08-21 修正：扫描件此前恒为 uncertain（design/29 §3.1 最初版本
    视觉判定 0/7），改进后（送前几页原生分辨率图 + 修正提示词）复测 8/8，
    接口现在会真的调用视觉分类器——但离线测试不能依赖环境里有没有配置
    真实 DASHSCOPE_API_KEY（跟本项目"offline 测试不吃真实网络"的既有教训
    一致，见 test_paddle_quote_api_e2e.py 的 text_call mock 先例）。这里
    显式把 get_scanned_classify_call 打成 None，测的是"没配视觉客户端时
    优雅退化成 uncertain，不崩"这个契约，不是"扫描件恒为 uncertain"这个
    已经不成立的旧设计。真实视觉判定准确率见
    test_scanned_pdf_classify.py::TestScannedPdfRealAccuracy（fresh e2e）。"""
    monkeypatch.setattr(
        "apps.api.intelligence.scanned_pdf_classify.get_scanned_classify_call",
        lambda: None,
    )
    path = DOCS / "bid/上海绵存投标文件.pdf"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_layer"] == "scanned"
    assert body["verdict"] == "uncertain"


def test_classify_unsupported_extension(client):
    r = client.post("/api/intake/classify-tier0",
                     files={"file": ("notes.txt", b"irrelevant content", "text/plain")})
    assert r.status_code == 200, r.text  # 不支持的类型也是正常回答，不是 4xx/5xx
    body = r.json()
    assert body["kind"] == "unsupported"
    assert body["verdict"] == "unsupported"


def test_classify_empty_file_rejected(client):
    r = client.post("/api/intake/classify-tier0",
                     files={"file": ("empty.xlsx", b"", "application/vnd.ms-excel")})
    assert r.status_code == 400
