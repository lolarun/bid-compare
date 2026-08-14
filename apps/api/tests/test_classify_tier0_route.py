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
    path = DOCS / "tender/金桥地体上盖招标文件.pdf"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf"
    assert body["verdict"] == "document"
    assert body["text_layer"] == "native"


def test_classify_scanned_pdf(client):
    path = DOCS / "bid/上海绵存投标文件.pdf"
    _skip_if_missing(path)
    with path.open("rb") as fh:
        r = client.post("/api/intake/classify-tier0", files={"file": (path.name, fh, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_layer"] == "scanned"


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
