"""record_vl_snapshots.py — 录制 VL-direct 重放基线。

## 重放到底测什么

模型输出本身不确定（同份同配置跑出的行数、页码、金额都会变），所以**模型不是
被测对象**。被测的是它下游那条确定性链路：CSV 解析 → 列名映射 → 结构门 →
ExtractionDraft。把模型那一次的原始 CSV 冻住，这条链路就完全可重放。

因此快照只存两样模型给的东西：**原始 CSV** 与**那次用的旋转表**。渲染不存——
渲染是确定的，且不影响这条链路（模型看到什么已经固化在 CSV 里了）。

## 为什么要存提示词哈希

提示词改了，旧快照就是在拿旧格式的输入验证新解析器——测试还绿着，但验证的东西
已经不存在了。重放时哈希不匹配必须**失败**而不是跳过（`.claude/rules/tests.md`：
replay 缓存 miss 必须使测试失败，禁止假绿）。

## 不要为了让测试变绿而重录

录到什么就是什么。若某次方向判错导致快照很差，那是这条链路的真实表现，应当如实
记录并去修方向，而不是重摇到绿（CLAUDE.md §8 明确禁止反复重写一条已通过的链路）。

用法：
    python scripts/record_vl_snapshots.py --doc 上海浦东 --doc 亨通
    python scripts/record_vl_snapshots.py --all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apps.api.core.config import get_settings  # noqa: E402
from apps.api.intelligence import vl_quote as vd  # noqa: E402

OUT_DIR = REPO / "tests" / "fixtures" / "vl_snapshots"

# slug → (文件名关键字, 所在目录)
DOCS = {
    "quote_cable_pudong": ("上海浦东", "tests/fixtures/documents"),
    "quote_cable_hengtong": ("亨通", "tests/fixtures/documents"),
    "quote_cable_hongsheng": ("宏胜", "tests/fixtures/documents"),
    "quote_cable_yuandong": ("远东", "tests/fixtures/documents"),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def derive_expected(snap: dict) -> dict:
    """从快照的 CSV 推导"重放应当得到什么" —— 离线，不打 API。

    这是**特征化基线**（characterization baseline）：它钉住的是当前行为，
    包含当前的缺陷。它的作用不是"证明结果正确"，而是"结果一旦变化必须有人知道"。
    确定性链路改了 → 这里红 → 要么是改坏了，要么是有意改进、需显式刷新基线。
    """
    import collections

    from apps.api.intelligence.vl_quote import build_draft

    d = build_draft(
        snap["csv"], file_path=snap["pdf"], page_count=snap["page_count"],
        processed_pages=snap["processed_pages"],
        rotations={int(k): v for k, v in (snap.get("rotations") or {}).items()},
        unresolved_pages=snap.get("unresolved_pages") or [],
    )
    diag = d.meta.get("diagnostics") or {}
    align = diag.get("alignment") or {}
    seq = diag.get("sequence") or {}
    lines = [r for r in d.rows if r.row_type == "quote_line"]
    return {
        "quality_status": d.quality.status,
        "blocking_reasons": sorted(d.quality.blocking_reasons or []),
        "row_counts": dict(collections.Counter(r.row_type for r in d.rows)),
        "alignment": {k: align.get(k) for k in
                      ("verdict", "extra_cell_rows", "missing_cell_rows", "header_len")},
        "sequence": {k: seq.get(k) for k in ("verdict", "coverage", "missing_count")},
        "copies": dict(collections.Counter(
            (r.fields.get("copy_no") or "").strip() for r in lines)),
        "has_price_column": diag.get("has_price_column"),
        "rows_without_page": diag.get("rows_without_page"),
        "column_shift_rows": sum(1 for r in d.rows if "column_shift" in r.validation_flags),
    }


def refresh_expected() -> None:
    """给已有快照补/更新 expected 块。**不重新调模型**——只重放已冻结的 CSV。"""
    for f in sorted(OUT_DIR.glob("*.json")):
        snap = json.loads(f.read_text(encoding="utf-8"))
        snap["expected"] = derive_expected(snap)
        f.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        e = snap["expected"]
        print(f"{f.stem}: {e['quality_status']} 行 {e['row_counts']} "
              f"对齐 {e['alignment']['verdict']} 序号 {e['sequence']['verdict']}")


def record(slug: str, votes: int) -> dict:
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider

    name, folder = DOCS[slug]
    pdf = next((REPO / folder).glob(f"*{name}*.pdf"))
    cfg = get_settings()
    prov = DashScopeOCRProvider()
    captured: dict = {}
    t0 = time.time()

    def vl(imgs, prompt):
        text = prov.vl_extract_csv(imgs, prompt, model=cfg.DASHSCOPE_QUOTE_VL_MODEL)
        captured["csv"] = text
        return text

    def orient(parts, prompt):
        return prov.vl_extract_csv(
            [b for _t, b in parts], prompt,
            model=cfg.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts])

    draft = vd.recognize_quote_vl(str(pdf), vl_call=vl, orient_call=orient, votes=votes)

    snap = {
        "slug": slug,
        "doc": name,
        "pdf": pdf.name,
        "pdf_sha256": _file_sha(pdf),
        "page_count": draft.page_count,
        "processed_pages": list(draft.target_pages),
        # 模型给的两样东西 —— 重放的全部输入
        "csv": captured.get("csv", ""),
        "rotations": {str(k): v for k, v in (draft.meta.get("rotations") or {}).items()},
        "unresolved_pages": list(draft.meta.get("orientation_unresolved") or []),
        # provenance：少一样都会让"这批数字是怎么来的"变成猜测
        "recognizer": "vl_direct",
        "model": cfg.DASHSCOPE_QUOTE_VL_MODEL,
        "orient_model": cfg.DASHSCOPE_QUOTE_ORIENT_MODEL,
        "orient_votes": votes,
        "prompt_sha256": _sha(vd.PROMPT_QUOTE_CSV),
        "orient_prompt_sha256": _sha(vd.PROMPT_ORIENT),
        "seconds": round(time.time() - t0, 1),
    }
    snap["expected"] = derive_expected(snap)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{slug}.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")

    det = sum(1 for r in draft.rows if r.row_type == "quote_line")
    print(f"[{slug}] {det} 明细 / {len(draft.rows)} 行 / {draft.quality.status} / "
          f"旋转 {len(snap['rotations'])} 页 / 未决 {len(snap['unresolved_pages'])} / "
          f"{snap['seconds']}s", flush=True)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", action="append", help="slug 或中文名，可重复")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--refresh-expected", action="store_true",
                    help="只重放已有快照、刷新 expected 块，不调模型")
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()

    if a.refresh_expected:
        refresh_expected()
        return 0
    if a.all:
        slugs = list(DOCS)
    elif a.doc:
        slugs = [s if s in DOCS else next(k for k, v in DOCS.items() if v[0] == s)
                 for s in a.doc]
    else:
        ap.error("需要 --doc 或 --all")

    cfg = get_settings()
    print(f"录制 VL 重放基线｜模型 {cfg.DASHSCOPE_QUOTE_VL_MODEL}｜"
          f"方向 {cfg.DASHSCOPE_QUOTE_ORIENT_MODEL} × {a.votes} 轮｜并行 {a.jobs}")
    print(f"提示词哈希 {_sha(vd.PROMPT_QUOTE_CSV)}｜输出 {OUT_DIR}")

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(lambda s: record(s, a.votes), slugs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
