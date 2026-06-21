"""test_e2e_snapshot.py — 确定性 OCR/LLM 快照回归（PR 必跑层，不打真实 API）。

与 test_e2e_extraction.py（@e2e，在线、非确定）分工不同：
本测试从 tests/fixtures/ocr_snapshots/<doc>.json 重放，验证识别管线在固定 OCR/LLM 输入下
**完全确定**——TableGrid / tiling / dedup / 质量门 的回归由它守护，不受模型随机性影响。

来源（§13）：真实 OCR HTML/JSON 快照 → 解析/合并/去重 → ExtractionDraft。
快照由 scripts/run_baseline.py（record 模式）生成；缺失则 skip。

注意：replay 模式下 extract_meta（supplier_name/declared_total）不可用（不缓存），
故只对**行级**抽取（seq、字段）做确定性断言，不对文档 meta 断言。

路径约定：
  快照来源：tests/fixtures/ocr_snapshots/<doc>.json（由 run_baseline.py 生成）
  outputs/ocr_snapshots/ 是过期副本，不用于测试。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
# 与 run_baseline.py 保持一致：tests/fixtures/ocr_snapshots
SNAP_DIR = REPO / "tests" / "fixtures" / "ocr_snapshots"
DOCS = REPO / "docs" / "test"

# doc_name → pdf
SNAPSHOT_DOCS = {
    "quote_taikelong": DOCS / "泰科龙投标文件.pdf",
    "quote_miancun": DOCS / "上海绵存投标文件.pdf",
    "quote_kaishuo": DOCS / "凯硕新正投标文件.pdf",
}


def _available():
    return [(n, p) for n, p in SNAPSHOT_DOCS.items()
            if (SNAP_DIR / f"{n}.json").exists() and p.exists()]


def _seqs_and_fields(draft):
    """提取 (seq → 关键字段) 的稳定快照，用于确定性比对。"""
    out = {}
    for r in draft.rows:
        if r.row_type != "quote_line":
            continue
        seq = str(r.fields.get("seq") or "").strip()
        if seq.isdigit():
            f = r.fields
            out[seq] = (
                f.get("name"), f.get("spec"), f.get("qty"),
                f.get("total_price") or f.get("total_price_incl_tax"),
                f.get("tax_rate"),
            )
    return out


@pytest.mark.skipif(not _available(), reason="无 OCR 快照（先跑 scripts/run_baseline.py）")
@pytest.mark.parametrize("doc_name,pdf", _available())
def test_snapshot_replay_deterministic(doc_name, pdf):
    """同一快照重放两次，行级抽取必须完全一致（确定性证明）。"""
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    snap = SNAP_DIR / f"{doc_name}.json"
    adapter = _get_quote_adapter()

    p1 = SnapshotProvider(None, snap, mode="replay")
    d1 = recognize_tables(str(pdf), p1, adapter)

    # ── 严格 replay 检查：任何目标页失败都必须被 BLOCKED ──────────────────
    # quality.failed_target_pages 非空 → BLOCKED；假绿在此暴露。
    assert d1.quality.failed_target_pages == [], (
        f"{doc_name}: replay 中目标页失败（缓存缺失？）: "
        f"{d1.quality.failed_target_pages}"
    )

    p2 = SnapshotProvider(None, snap, mode="replay")
    d2 = recognize_tables(str(pdf), p2, adapter)
    assert d2.quality.failed_target_pages == []

    s1, s2 = _seqs_and_fields(d1), _seqs_and_fields(d2)
    assert s1 == s2, f"{doc_name}: 重放两次结果不一致（管线非确定）"
    # 质量门状态也必须确定
    assert d1.quality.status == d2.quality.status
    assert d1.quality.quote_line_count == d2.quality.quote_line_count


@pytest.mark.skipif(not _available(), reason="无 OCR 快照")
@pytest.mark.parametrize("doc_name,pdf", _available())
def test_snapshot_replay_no_silent_truncation(doc_name, pdf):
    """重放结果必须全页处理、无静默截断（§3.1）。"""
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    snap = SNAP_DIR / f"{doc_name}.json"
    provider = SnapshotProvider(None, snap, mode="replay")
    draft = recognize_tables(str(pdf), provider, _get_quote_adapter())
    assert not draft.quality.truncated
    assert draft.quality.total_pages > 0
    # 无目标页失败（保证"无静默截断"不被 page-level 异常绕过）
    assert draft.quality.failed_target_pages == [], (
        f"{doc_name}: 目标页失败 → 静默截断: {draft.quality.failed_target_pages}"
    )


# ── 负向测试：故意破坏一个 LLM cache key → 测试必须失败 ─────────────────────

@pytest.mark.skipif(not _available(), reason="无 OCR 快照")
def test_snapshot_replay_fails_on_missing_llm_key():
    """删除快照中任意一条 LLM 缓存 key，replay 必须让 quality.failed_target_pages 非空。

    用途：确保 'replay 假绿' 不再发生（过期快照导致页面静默失败而测试仍绿）。
    """
    import tempfile, os
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    # 用第一个可用文档
    doc_name, pdf = _available()[0]
    snap = SNAP_DIR / f"{doc_name}.json"

    # 载入快照并清空所有 LLM 条目 → 保证所有目标页 LLM 调用均缺失
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data.get("llm"), f"{doc_name}: 快照无 LLM 条目"
    corrupted = copy.deepcopy(data)
    corrupted["llm"] = {}   # 清空全部 LLM 缓存

    # 写到临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        json.dump(corrupted, tf, ensure_ascii=False)
        tmp_path = Path(tf.name)

    try:
        provider = SnapshotProvider(None, tmp_path, mode="replay")
        draft = recognize_tables(str(pdf), provider, _get_quote_adapter())
        # 删掉 key 后，必须有至少一个目标页失败
        assert draft.quality.failed_target_pages, (
            "破坏 LLM key 后 failed_target_pages 仍为空 — replay 假绿未修复"
        )
        assert draft.quality.status == "BLOCKED", (
            "破坏 LLM key 后 quality.status 应为 BLOCKED"
        )
    finally:
        tmp_path.unlink(missing_ok=True)
