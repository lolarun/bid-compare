"""Unit tests for the QwenVL provider — JSON parser strictness + candidates."""

import json

import pytest

from apps.api.intelligence.base import ExtractionResponse, ProviderError
from apps.api.intelligence.providers.qwen_vl import QwenVLProvider
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT


class TestParseJsonStrict:
    def test_plain_json(self):
        out = QwenVLProvider._parse_json_strict('{"a": 1}')
        assert out == {"a": 1}

    def test_with_markdown_fence(self):
        text = '```json\n{"a": 1, "b": "hello"}\n```'
        assert QwenVLProvider._parse_json_strict(text) == {"a": 1, "b": "hello"}

    def test_with_bare_fence(self):
        text = '```\n{"x": [1, 2]}\n```'
        assert QwenVLProvider._parse_json_strict(text) == {"x": [1, 2]}

    def test_prefix_explanation(self):
        text = '这是提取结果：{"items": []}'
        assert QwenVLProvider._parse_json_strict(text) == {"items": []}

    def test_suffix_text(self):
        text = '{"ok": true}\n注：以上是结果'
        assert QwenVLProvider._parse_json_strict(text) == {"ok": True}

    def test_nested(self):
        text = '```json\n{"items":[{"n":"DN100","q":12}]}\n```'
        assert QwenVLProvider._parse_json_strict(text) == {
            "items": [{"n": "DN100", "q": 12}]
        }

    def test_array_top_level_rejected(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            QwenVLProvider._parse_json_strict("[1, 2, 3]")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            QwenVLProvider._parse_json_strict("")


class TestCandidateBuilder:
    def test_user_model_first(self, monkeypatch):
        monkeypatch.delenv("LLM_VISION_MODEL", raising=False)
        monkeypatch.delenv("LLM_VISION_MODEL_FALLBACK", raising=False)
        c = QwenVLProvider._build_candidates(model="my-model", models=None)
        assert c[0] == "my-model"
        assert "qwen3-vl-plus" in c

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_VISION_MODEL", "preferred-x")
        monkeypatch.setenv("LLM_VISION_MODEL_FALLBACK", "fallback-y")
        c = QwenVLProvider._build_candidates(model=None, models=None)
        assert "preferred-x" in c
        assert "fallback-y" in c

    def test_dedup(self, monkeypatch):
        monkeypatch.setenv("LLM_VISION_MODEL", "qwen3-vl-plus")
        c = QwenVLProvider._build_candidates(model="qwen3-vl-plus", models=None)
        # No duplicates
        assert len(c) == len(set(c))

    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(ProviderError):
            QwenVLProvider(api_key=None)


class TestMockProvider:
    def test_tender_minimal(self):
        m = MockProvider()
        resp = m.extract([b"x"], TENDER_SCHEMA, TENDER_PROMPT)
        assert isinstance(resp, ExtractionResponse)
        assert "project_name" in resp.data
        assert "items" in resp.data
        assert resp.provider == "mock"

    def test_quote_minimal(self):
        m = MockProvider()
        resp = m.extract([b"x"], QUOTE_SCHEMA, QUOTE_PROMPT)
        assert "items" in resp.data

    def test_canned_passthrough(self):
        canned = {"project_name": "T", "items": [{"name": "A"}]}
        m = MockProvider(canned=canned)
        resp = m.extract([b"x"], TENDER_SCHEMA, TENDER_PROMPT)
        assert resp.data == canned

    def test_fixture_loading(self, fixture_dir):
        m = MockProvider(fixture_dir=fixture_dir)
        # The fixture mock_tender.json is keyed by `tender` since TENDER_SCHEMA
        # contains "project_name" in required list.
        resp = m.extract([b"x"], TENDER_SCHEMA, TENDER_PROMPT)
        assert resp.data["project_name"].startswith("测试招标")
        assert len(resp.data["items"]) == 3
