"""视觉页面分类单元测试（P0）—— 无 API。

覆盖：from_dict 健壮性、Flash→Plus 升级条件、_classify_pages 升级流程、
角色路由集合、SnapshotProvider 视觉缓存 key 随模型/prompt 变化。
"""
from __future__ import annotations

import io

from PIL import Image

from apps.api.intelligence.page_classifier import (
    VisualPageRole, VisualPageClassification,
    QUOTE_TARGET_ROLES, TENDER_TARGET_ROLES, META_ROLES, OCR_SKIP_ROLES,
)
from apps.api.intelligence.table_recognizer import _needs_review, _classify_pages


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


# ── from_dict 健壮性 ────────────────────────────────────────────────────────

def test_from_dict_illegal_role_falls_back_unknown():
    c = VisualPageClassification.from_dict({"page": 3, "role": "garbage", "confidence": 0.9})
    assert c.role == VisualPageRole.UNKNOWN


def test_from_dict_illegal_orientation_zeroed():
    c = VisualPageClassification.from_dict({"page": 1, "role": "cover", "orientation": 45})
    assert c.orientation == 0


def test_from_dict_valid_orientation_kept():
    c = VisualPageClassification.from_dict({"page": 1, "role": "quote_table_header", "orientation": 90})
    assert c.orientation == 90 and c.role == VisualPageRole.QUOTE_TABLE_HEADER


# ── 路由集合不重叠/正确 ─────────────────────────────────────────────────────

def test_role_routing_sets_disjoint():
    assert QUOTE_TARGET_ROLES.isdisjoint(OCR_SKIP_ROLES)
    assert TENDER_TARGET_ROLES.isdisjoint(OCR_SKIP_ROLES)
    assert META_ROLES.isdisjoint(QUOTE_TARGET_ROLES | TENDER_TARGET_ROLES)


# ── _needs_review 升级条件 ──────────────────────────────────────────────────

def _c(role, conf=0.99, table=True, cont_prev=None, mixed=False):
    return VisualPageClassification(
        page=2, role=role, confidence=conf, contains_table=table, mixed_content=mixed)


def test_review_low_confidence():
    c = _c(VisualPageRole.QUOTE_TABLE_HEADER, conf=0.5)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is True


def test_review_unknown_role():
    c = _c(VisualPageRole.UNKNOWN)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is True


def test_review_role_table_but_no_table_flag():
    c = _c(VisualPageRole.QUOTE_TABLE_HEADER, table=False)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is True


def test_review_contains_table_but_technical():
    c = _c(VisualPageRole.TECHNICAL_SPEC, table=True)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is True


def test_review_continuation_without_prior_table():
    prev = _c(VisualPageRole.TECHNICAL_SPEC)
    cur = _c(VisualPageRole.QUOTE_TABLE_CONTINUATION)
    assert _needs_review(cur, [prev, cur], 1, QUOTE_TARGET_ROLES) is True


def test_review_continuation_with_prior_table_ok():
    prev = _c(VisualPageRole.QUOTE_TABLE_HEADER)
    cur = _c(VisualPageRole.QUOTE_TABLE_CONTINUATION)
    assert _needs_review(cur, [prev, cur], 1, QUOTE_TARGET_ROLES) is False


def test_review_mixed_content():
    c = _c(VisualPageRole.QUOTE_TABLE_HEADER, mixed=True)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is True


def test_review_clean_high_conf_header_ok():
    c = _c(VisualPageRole.QUOTE_TABLE_HEADER, conf=0.95)
    assert _needs_review(c, [c], 0, QUOTE_TARGET_ROLES) is False


# ── _classify_pages：flash + 选择性 plus 复判 ───────────────────────────────

class FakeVisualProvider:
    def __init__(self, flash, review=None):
        self.flash = flash
        self.review = review or {}
        self.review_calls: list[int] = []

    def classify_pages_visual(self, thumbnails, doc_type, **kw):
        return self.flash, []

    def review_pages_visual(self, page_image, neighbor_thumbs, flash_result, page_no, **kw):
        self.review_calls.append(page_no)
        return self.review.get(page_no, {**flash_result, "source": "plus"})


def test_classify_pages_escalates_only_low_conf():
    flash = [
        {"page": 1, "role": "cover", "confidence": 0.95, "contains_table": False},
        {"page": 2, "role": "quote_table_header", "confidence": 0.95, "contains_table": True},
        {"page": 3, "role": "quote_table_continuation", "confidence": 0.4, "contains_table": True},  # low → review
    ]
    review = {3: {"page": 3, "role": "quote_table_continuation", "confidence": 0.9,
                  "contains_table": True, "source": "plus"}}
    prov = FakeVisualProvider(flash, review)
    thumbs = [_png(), _png(), _png()]
    cls, n_flash, n_plus = _classify_pages(prov, thumbs, thumbs, "quote")
    assert n_flash == 3
    assert n_plus == 1 and prov.review_calls == [3]
    assert cls[2].confidence == 0.9 and cls[2].source == "plus"


def test_classify_pages_no_escalation_when_all_clean():
    flash = [
        {"page": 1, "role": "cover", "confidence": 0.95, "contains_table": False},
        {"page": 2, "role": "quote_table_header", "confidence": 0.95, "contains_table": True},
    ]
    prov = FakeVisualProvider(flash)
    thumbs = [_png(), _png()]
    cls, _n, n_plus = _classify_pages(prov, thumbs, thumbs, "quote")
    assert n_plus == 0 and prov.review_calls == []


# ── SnapshotProvider 视觉缓存 key 随模型/prompt 变化 ────────────────────────

def _make_inner():
    class Inner:
        def __init__(self):
            self.calls = 0
        def classify_pages_visual(self, thumbnails, doc_type, *, model, prompt_version,
                                  batch_size, overlap, temperature, max_pixels):
            self.calls += 1
            return [{"page": 1, "role": "cover", "confidence": 1.0}], []
    return Inner()


def test_visual_cache_key_changes_with_model(tmp_path):
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    inner = _make_inner()
    sp = SnapshotProvider(inner, tmp_path / "s.json", mode="record")
    thumbs = [_png()]
    sp.classify_pages_visual(thumbs, "quote", model="qwen3-vl-flash", prompt_version="v1")
    sp.classify_pages_visual(thumbs, "quote", model="qwen3-vl-flash", prompt_version="v1")  # cached
    assert inner.calls == 1
    sp.classify_pages_visual(thumbs, "quote", model="qwen3-vl-plus", prompt_version="v1")    # model差→miss
    assert inner.calls == 2
    sp.classify_pages_visual(thumbs, "quote", model="qwen3-vl-flash", prompt_version="v2")   # ver差→miss
    assert inner.calls == 3


def test_visual_cache_key_changes_with_temperature(tmp_path):
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    inner = _make_inner()
    sp = SnapshotProvider(inner, tmp_path / "s.json", mode="record")
    thumbs = [_png()]
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", temperature=0.0)
    assert inner.calls == 1
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", temperature=0.0)  # hit
    assert inner.calls == 1
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", temperature=0.5)  # miss
    assert inner.calls == 2


def test_visual_cache_key_changes_with_max_pixels(tmp_path):
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    inner = _make_inner()
    sp = SnapshotProvider(inner, tmp_path / "s.json", mode="record")
    thumbs = [_png()]
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", max_pixels=2_000_000)
    assert inner.calls == 1
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", max_pixels=2_000_000)  # hit
    assert inner.calls == 1
    sp.classify_pages_visual(thumbs, "quote", model="m", prompt_version="v1", max_pixels=4_000_000)  # miss
    assert inner.calls == 2


def test_visual_replay_missing_raises(tmp_path):
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    (tmp_path / "s.json").write_text('{"ocr":{},"llm":{},"meta":{},"visual":{},"failures":{}}',
                                     encoding="utf-8")
    sp = SnapshotProvider(None, tmp_path / "s.json", mode="replay")
    import pytest
    with pytest.raises(KeyError):
        sp.classify_pages_visual([_png()], "quote")


# ── v3 语义字段 from_dict 健壮性 ────────────────────────────────────────────

def test_from_dict_new_semantic_fields_parsed():
    c = VisualPageClassification.from_dict({
        "page": 5, "role": "quote_table_continuation", "confidence": 0.9,
        "has_line_items": True, "estimated_line_item_count": 12,
        "has_column_header": False, "has_total_row": True,
        "table_structure_continues": True,
    })
    assert c.has_line_items is True
    assert c.estimated_line_item_count == 12
    assert c.has_column_header is False
    assert c.has_total_row is True
    assert c.table_structure_continues is True


def test_from_dict_new_semantic_fields_default_none():
    c = VisualPageClassification.from_dict({"page": 1, "role": "cover", "confidence": 0.9})
    assert c.has_line_items is None
    assert c.has_total_row is None
    assert c.table_structure_continues is None


# ── §2 触发条件：只当 has_line_items is None 时升级 ──────────────────────────

def test_review_subtotal_after_cont_unknown_line_items():
    """§2：续表后 subtotal，has_line_items=None → 触发 Plus"""
    prev = _c(VisualPageRole.QUOTE_TABLE_CONTINUATION)
    cur = VisualPageClassification(
        page=3, role=VisualPageRole.SUBTOTAL_OR_SUMMARY,
        confidence=0.92, contains_table=False, has_line_items=None)
    assert _needs_review(cur, [prev, cur], 1, QUOTE_TARGET_ROLES) is True


def test_review_subtotal_after_cont_has_items_true_no_trigger():
    """§2：has_line_items=True 语义已明确，不触发 Plus（语义覆写层直接处理）"""
    prev = _c(VisualPageRole.QUOTE_TABLE_CONTINUATION)
    cur = VisualPageClassification(
        page=3, role=VisualPageRole.SUBTOTAL_OR_SUMMARY,
        confidence=0.92, contains_table=False, has_line_items=True)
    assert _needs_review(cur, [prev, cur], 1, QUOTE_TARGET_ROLES) is False


def test_review_subtotal_after_cont_has_items_false_no_trigger():
    """§2：has_line_items=False 语义明确（真实小计页），不触发 Plus"""
    prev = _c(VisualPageRole.QUOTE_TABLE_CONTINUATION)
    cur = VisualPageClassification(
        page=3, role=VisualPageRole.SUBTOTAL_OR_SUMMARY,
        confidence=0.92, contains_table=False, has_line_items=False)
    assert _needs_review(cur, [prev, cur], 1, QUOTE_TARGET_ROLES) is False


# ── Phase 3 语义覆写：has_line_items=True 时 subtotal→continuation ────────────

def test_classify_pages_semantic_override_subtotal_to_cont():
    """Phase 3：flash 判 subtotal 但 has_line_items=True → 语义覆写为 continuation"""
    flash = [
        {"page": 1, "role": "quote_table_header", "confidence": 0.95,
         "contains_table": True, "has_line_items": True},
        {"page": 2, "role": "quote_table_continuation", "confidence": 0.95,
         "contains_table": True, "has_line_items": True},
        {"page": 3, "role": "subtotal_or_summary", "confidence": 0.9,
         "contains_table": False, "has_line_items": True, "has_total_row": True},
    ]
    prov = FakeVisualProvider(flash)
    thumbs = [_png(), _png(), _png()]
    cls, _, n_plus = _classify_pages(prov, thumbs, thumbs, "quote")
    assert n_plus == 0  # §2 不触发（has_line_items 已知），无 Plus 调用
    assert cls[2].role == VisualPageRole.QUOTE_TABLE_CONTINUATION
    assert "§2-semantic" in " ".join(cls[2].evidence)


def test_classify_pages_subtotal_false_not_overridden():
    """Phase 3：has_line_items=False 时 subtotal 保持不变"""
    flash = [
        {"page": 1, "role": "quote_table_header", "confidence": 0.95, "contains_table": True},
        {"page": 2, "role": "subtotal_or_summary", "confidence": 0.95,
         "contains_table": False, "has_line_items": False},
    ]
    prov = FakeVisualProvider(flash)
    thumbs = [_png(), _png()]
    cls, _, _ = _classify_pages(prov, thumbs, thumbs, "quote")
    assert cls[1].role == VisualPageRole.SUBTOTAL_OR_SUMMARY


def test_classify_pages_debug_captures_three_layers():
    """_debug 参数正确捕获三阶段快照"""
    flash = [
        {"page": 1, "role": "quote_table_header", "confidence": 0.95,
         "contains_table": True, "has_line_items": True},
        {"page": 2, "role": "subtotal_or_summary", "confidence": 0.4,  # low conf → Plus
         "contains_table": False, "has_line_items": True, "has_total_row": True},
    ]
    review = {2: {"page": 2, "role": "subtotal_or_summary", "confidence": 0.9,
                  "contains_table": False, "has_line_items": True, "source": "plus"}}
    prov = FakeVisualProvider(flash, review)
    thumbs = [_png(), _png()]
    debug: dict = {}
    cls, _, n_plus = _classify_pages(prov, thumbs, thumbs, "quote", _debug=debug)

    assert "flash" in debug and "after_plus" in debug and "final" in debug
    # Flash layer: p2 was subtotal
    assert debug["flash"][1].role == VisualPageRole.SUBTOTAL_OR_SUMMARY
    # After Plus: still subtotal (Plus confirmed subtotal, no override yet)
    assert debug["after_plus"][1].role == VisualPageRole.SUBTOTAL_OR_SUMMARY
    # Final: has_line_items=True overrides to continuation
    assert debug["final"][1].role == VisualPageRole.QUOTE_TABLE_CONTINUATION
    assert n_plus >= 1  # At minimum p2 was reviewed (low confidence)
