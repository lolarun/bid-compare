"""docs/design/26 P4 补：文档级标量/要求的 Paddle 文字层抽取测试。

不打网络——`text_call` 全部用假实现注入，只验证编排（拼上下文、调用、解析）
正确，解析本身的正确性已经由 `vl_tender.py` 既有测试覆盖（`parse_tender_meta`/
`parse_requirements` 原样复用，不重写）。
"""
from __future__ import annotations

from apps.api.intelligence.paddle_doc_meta import (
    DEFAULT_QUOTE_REQUIREMENTS,
    extract_meta_from_text,
    extract_requirements_from_text,
)
from apps.api.intelligence.vl_tender import DEFAULT_TENDER_REQUIREMENTS


def test_extract_meta_from_text_parses_key_value_response():
    calls = []

    def fake_call(prompt: str) -> str:
        calls.append(prompt)
        return "project_name: 某研发及商业项目\nproject_code: \ntender_date: 2026年1月16日\ndeadline: "

    meta = extract_meta_from_text(["封面文字内容"], fake_call)
    assert meta["project_name"] == "某研发及商业项目"
    assert meta["tender_date"] == "2026年1月16日"
    assert meta["project_code"] == ""
    assert len(calls) == 1
    assert "封面文字内容" in calls[0]


def test_extract_meta_from_text_empty_pages_returns_blank_without_calling():
    calls = []
    meta = extract_meta_from_text([], lambda p: calls.append(p) or "")
    assert meta == {"project_name": "", "project_code": "", "tender_date": "", "deadline": ""}
    assert calls == []


def test_extract_meta_from_text_exception_does_not_propagate():
    def boom(prompt: str) -> str:
        raise RuntimeError("network down")

    meta = extract_meta_from_text(["文字"], boom)
    assert meta == {"project_name": "", "project_code": "", "tender_date": "", "deadline": ""}


def test_extract_requirements_from_text_parses_sections():
    def fake_call(prompt: str) -> str:
        return (
            "### 业主品牌要求\nbrand_en,brand_cn\nKITZ,开滋\n\n"
            "### 投标单位参与品牌\nsupplier_name,brand\n某某机电,开滋\n\n"
            "### 材料类别\n阀门"
        )

    out = extract_requirements_from_text(["文字"], fake_call, DEFAULT_TENDER_REQUIREMENTS)
    assert out["brand_requirement"] == [{"brand_en": "KITZ", "brand_cn": "开滋"}]
    assert out["supplier_brands"] == [{"supplier_name": "某某机电", "brand": "开滋"}]
    assert out["material_class"] == "阀门"


def test_extract_requirements_from_text_empty_reqs_short_circuits():
    calls = []
    out = extract_requirements_from_text(["文字"], lambda p: calls.append(p) or "", ())
    assert out == {}
    assert calls == []


def test_extract_requirements_from_text_prompt_includes_context_and_declared_titles():
    seen = {}

    def fake_call(prompt: str) -> str:
        seen["prompt"] = prompt
        return ""

    extract_requirements_from_text(["OCR文字内容"], fake_call, DEFAULT_QUOTE_REQUIREMENTS)
    assert "OCR文字内容" in seen["prompt"]
    assert "是否包含投标价格" in seen["prompt"]


def test_default_quote_requirements_has_price_included_key():
    keys = {r.key for r in DEFAULT_QUOTE_REQUIREMENTS}
    assert "price_included" in keys
