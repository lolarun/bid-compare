"""Phase 2: match_anchors_topk recall tests (no LLM, embedding mocked).

Pins that Top-K candidate generation:
  - returns up to K candidates per quote, ordered by combined score
  - excludes DN-mismatch and canonical hard-conflict (0.0) candidates
  - reuses pre-embedded anchor_vecs when provided
  - leaves the legacy match_anchors argmax behavior unchanged
"""
from dataclasses import dataclass, field

import apps.api.services.alignment.anchor_match as am


@dataclass
class _Anchor:
    seq: int
    name: str
    spec: str = ""
    pressure: str = ""
    materials: dict = field(default_factory=dict)
    canonical: dict = field(default_factory=dict)

    def material_text(self) -> str:
        return " ".join(v for v in self.materials.values() if v)


@dataclass
class _Quote:
    id: int


def _patch_embed(monkeypatch, vec_map: dict[str, list[float]]):
    """Patch am._embed to return deterministic vectors keyed by text prefix."""
    def fake_embed(client, texts):
        out = []
        for t in texts:
            # match by first token (anchor/quote name)
            key = next((k for k in vec_map if t.startswith(k)), None)
            out.append(vec_map[key] if key else [0.0, 0.0, 0.0])
        return out
    monkeypatch.setattr(am, "_embed", fake_embed)


def test_topk_returns_sorted_candidates(monkeypatch):
    """Three anchors all plausible → Top-K ordered by combined score desc."""
    anchors = [
        _Anchor(seq=1, name="球阀A", spec="DN50"),
        _Anchor(seq=2, name="球阀B", spec="DN50"),
        _Anchor(seq=3, name="球阀C", spec="DN50"),
    ]
    quotes = [_Quote(id=10)]
    # quote vector closest to anchor1 > anchor2 > anchor3
    _patch_embed(monkeypatch, {
        "球阀A": [1.0, 0.0, 0.0],
        "球阀B": [0.9, 0.1, 0.0],
        "球阀C": [0.7, 0.3, 0.0],
        "球阀报价": [1.0, 0.0, 0.0],
    })
    topk = am.match_anchors_topk(
        anchors, quotes,
        quote_texts=["球阀报价 DN50"],
        quote_dns=[50],
        client=object(),  # not used (embed mocked)
        quote_canonicals=[{}],
        k=2,
    )
    assert len(topk) == 1
    cands = topk[0]
    assert len(cands) == 2, f"expected top-2, got {len(cands)}"
    # ordered by combined desc → anchor idx 0 first, then 1
    assert cands[0][0] == 0
    assert cands[1][0] == 1
    assert cands[0][1] >= cands[1][1]  # cosine desc


def test_topk_excludes_dn_mismatch(monkeypatch):
    """Anchor with different DN is filtered out even if cosine high."""
    anchors = [
        _Anchor(seq=1, name="球阀A", spec="DN100"),  # DN mismatch vs quote DN50
        _Anchor(seq=2, name="球阀B", spec="DN50"),
    ]
    quotes = [_Quote(id=10)]
    _patch_embed(monkeypatch, {
        "球阀A": [1.0, 0.0, 0.0],
        "球阀B": [0.95, 0.05, 0.0],
        "球阀报价": [1.0, 0.0, 0.0],
    })
    topk = am.match_anchors_topk(
        anchors, quotes,
        quote_texts=["球阀报价 DN50"],
        quote_dns=[50],
        client=object(),
        quote_canonicals=[{}],
        k=3,
    )
    cands = topk[0]
    assert all(ai == 1 for ai, _cos, _c in cands), f"DN100 anchor should be excluded, got {cands}"


def test_topk_excludes_canonical_hard_conflict(monkeypatch):
    """valve_type conflict (canonical_match_score 0.0) excludes the candidate."""
    anchors = [
        _Anchor(seq=1, name="闸阀A", spec="DN50", canonical={"valve_type": "闸阀", "dn": "DN50"}),
        _Anchor(seq=2, name="球阀B", spec="DN50", canonical={"valve_type": "球阀", "dn": "DN50"}),
    ]
    quotes = [_Quote(id=10)]
    _patch_embed(monkeypatch, {
        "闸阀A": [1.0, 0.0, 0.0],
        "球阀B": [0.99, 0.01, 0.0],
        "球阀报价": [1.0, 0.0, 0.0],
    })
    # quote is a 球阀 → conflicts with 闸阀 anchor (idx 0)
    topk = am.match_anchors_topk(
        anchors, quotes,
        quote_texts=["球阀报价 DN50"],
        quote_dns=[50],
        client=object(),
        quote_canonicals=[{"valve_type": "球阀", "dn": "DN50"}],
        k=3,
    )
    cands = topk[0]
    assert all(ai == 1 for ai, _cos, _c in cands), f"闸阀 anchor must be hard-blocked, got {cands}"


def test_topk_reuses_anchor_vecs(monkeypatch):
    """When anchor_vecs is supplied, anchors are NOT re-embedded."""
    anchors = [_Anchor(seq=1, name="球阀A", spec="DN50")]
    quotes = [_Quote(id=10)]

    embed_calls = {"texts": []}

    def counting_embed(client, texts):
        embed_calls["texts"].append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(am, "_embed", counting_embed)

    anchor_vecs = [[1.0, 0.0, 0.0]]
    am.match_anchors_topk(
        anchors, quotes,
        quote_texts=["球阀报价 DN50"],
        quote_dns=[50],
        client=object(),
        quote_canonicals=[{}],
        k=3,
        anchor_vecs=anchor_vecs,
    )
    # Only the quote texts should have been embedded, never the anchor texts
    flat = [t for batch in embed_calls["texts"] for t in batch]
    assert any("球阀报价" in t for t in flat)
    assert not any(t.startswith("球阀A") for t in flat), "anchors should not be re-embedded"


def test_match_anchors_argmax_unchanged(monkeypatch):
    """Legacy match_anchors still returns the single best (argmax) per quote."""
    anchors = [
        _Anchor(seq=1, name="球阀A", spec="DN50"),
        _Anchor(seq=2, name="球阀B", spec="DN50"),
    ]
    quotes = [_Quote(id=10)]
    _patch_embed(monkeypatch, {
        "球阀A": [0.9, 0.1, 0.0],
        "球阀B": [1.0, 0.0, 0.0],
        "球阀报价": [1.0, 0.0, 0.0],
    })
    res = am.match_anchors(
        anchors, quotes,
        quote_texts=["球阀报价 DN50"],
        quote_dns=[50],
        client=object(),
        quote_canonicals=[{}],
    )
    assert len(res) == 1
    qi, ai, cos = res[0]
    assert qi == 0 and ai == 1, f"best should be anchor idx 1, got {ai}"
