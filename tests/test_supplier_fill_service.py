"""Phase 4: supplier_fill_llm service assembly (mock LLM, no DB, no network).

Covers attach_topk → Tier-1 pre-pass → build_prompt → call_llm → validate wiring:
  - tier-1 auto-aligns canonical-exact + high-cos rows WITHOUT calling the LLM
  - the LLM only sees undecided rows; its assignments flow through validate
  - a malformed/raising LLM degrades that supplier to residue (no crash)
  - build_prompt lists every undecided quote_id and anchor seq
  - model routing picks the thinking model only on tier-3 triggers
"""
import json

import pytest

import apps.api.services.supplier_fill_llm as sfl
from apps.api.services.supplier_fill_llm import (
    AnchorView, SupplierQuoteRow, fill_one_supplier, build_prompt, _pick_model,
)


def _anchors():
    return [
        AnchorView(seq=1, name="球阀", spec="DN50", canonical={"valve_type": "球阀", "dn": "DN50"}),
        AnchorView(seq=2, name="闸阀", spec="DN80", canonical={"valve_type": "闸阀", "dn": "DN80"}),
    ]


class _FakeUsage:
    total_tokens = 42


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeClient:
    """Fake OpenAI-compatible client returning canned chat completions."""
    def __init__(self, content):
        self._content = content
        self.calls = 0
        self.chat = type("C", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        return _FakeResp(self._content)


def test_tier1_auto_aligns_without_llm(monkeypatch):
    """A canonical-exact, high-cos row is decided by code; LLM is not consulted."""
    anchors = _anchors()
    row = SupplierQuoteRow(
        quote_id=101, supplier_id=7, material="球阀", spec="DN50",
        unit_price=100.0, qty=10.0, total_price=1000.0,
        canonical={"valve_type": "球阀", "dn": "DN50"},
        topk=[(1, 0.95)],  # pre-set so attach_topk is a no-op
    )
    client = _FakeClient(json.dumps({"assignments": []}))
    res = fill_one_supplier([row], anchors, client, supplier_name="供应商甲", anchor_vecs=[[1.0]])

    assert client.calls == 0, "tier-1 row must not trigger an LLM call"
    assert len(res.cells) == 1
    assert res.cells[0].status == "quoted"
    assert res.cells[0].anchor_seq == 1


def test_llm_assignments_flow_through_validate(monkeypatch):
    """Undecided row → LLM assignment → validated cell with price from the row."""
    anchors = _anchors()
    # cos below tier-1 threshold → goes to the LLM
    row = SupplierQuoteRow(
        quote_id=202, supplier_id=7, material="球阀", spec="DN50",
        unit_price=150.0, qty=4.0, total_price=600.0,
        canonical={"valve_type": "球阀", "dn": "DN50"},
        topk=[(1, 0.66)],
    )
    canned = json.dumps({"assignments": [
        {"quote_id": 202, "anchor_seq": 1, "status": "quoted", "confidence": 0.8,
         "reason": "球阀 DN50 一致", "llm_unit_price": 9999.0}  # bogus price → must be ignored
    ]})
    client = _FakeClient(canned)
    res = fill_one_supplier([row], anchors, client, supplier_name="供应商甲", anchor_vecs=[[1.0]])

    assert client.calls == 1
    assert len(res.cells) == 1
    cell = res.cells[0]
    # price mismatch (9999 vs 150) → downgraded to pending, price still from row
    assert cell.status == "pending"
    assert cell.unit_price == 150.0
    assert "price_mismatch" in cell.flags
    assert res.tokens_used == 42


def test_llm_failure_degrades_to_residue(monkeypatch):
    """A raising LLM client must not crash; the supplier degrades to residue."""
    anchors = _anchors()
    row = SupplierQuoteRow(
        quote_id=303, supplier_id=7, material="球阀", spec="DN50",
        unit_price=100.0, qty=1.0, canonical={"valve_type": "球阀", "dn": "DN50"},
        topk=[(1, 0.6)],
    )

    class _Boom(_FakeClient):
        def create(self, **kwargs):
            raise RuntimeError("LLM down")

    client = _Boom("")
    res = fill_one_supplier([row], anchors, client, anchor_vecs=[[1.0]])
    assert res.error
    assert res.cells == []
    assert res.residue_quote_ids == [303]


def test_build_prompt_lists_all_ids():
    anchors = _anchors()
    rows = [
        SupplierQuoteRow(quote_id=11, supplier_id=7, material="球阀", spec="DN50", topk=[(1, 0.6)]),
        SupplierQuoteRow(quote_id=22, supplier_id=7, material="闸阀", spec="DN80", topk=[(2, 0.58)]),
    ]
    prompt = build_prompt(anchors, "供应商甲", rows, tier1_seqs=[])
    assert "quote_id=11" in prompt
    assert "quote_id=22" in prompt
    assert "#1" in prompt and "#2" in prompt
    assert "不得臆造" in prompt
    assert "不要输出价格" in prompt


def test_model_routing_thinking_on_close_candidates():
    rows = [SupplierQuoteRow(quote_id=1, supplier_id=7, topk=[(1, 0.80), (2, 0.79)])]  # gap 0.01 < 0.05
    assert _pick_model(rows, "qwen-plus", "qwen-think") == "qwen-think"

    rows2 = [SupplierQuoteRow(quote_id=1, supplier_id=7, topk=[(1, 0.80), (2, 0.50)])]  # gap 0.30
    assert _pick_model(rows2, "qwen-plus", "qwen-think") == "qwen-plus"

    # no thinking model configured → always default
    assert _pick_model(rows, "qwen-plus", None) == "qwen-plus"


def test_model_routing_thinking_on_high_amount():
    rows = [SupplierQuoteRow(quote_id=1, supplier_id=7, unit_price=10000.0, qty=10.0, topk=[(1, 0.9)])]
    assert _pick_model(rows, "qwen-plus", "qwen-think") == "qwen-think"  # 100000 >= 50000
