"""fresh_e2e.py — 多文档 fresh E2E 验证脚本（泰科龙 / 凯硕 / 绵存）。

功能：
  - 全 fresh 识别（独立临时快照，不碰官方 fixture）
  - 永久保存所有诊断资产到 tmp/fresh_e2e_{doc}_{timestamp}/
  - ExtractionDraft 完整 JSON + 每页 rows/source_ref/parser_mode/rotation_applied
  - 页面分类、target/recall 集合
  - OCR/LLM 快照（snapshot JSON）
  - 完整日志文件
  - 正确解析当前 chain orientation 日志格式
  - 对 Excel golden 逐行 diff，定位 extra / missing 行

禁止：
  - 覆盖官方 fixture（tests/fixtures/ocr_snapshots/*.json）
  - 读取官方 fixture（所有调用均走 fresh API）
  - 运行结束删除任何诊断资产

用法：
    python scripts/fresh_e2e.py taikelong          # 单文档
    python scripts/fresh_e2e.py kaishuo
    python scripts/fresh_e2e.py miancun
    python scripts/fresh_e2e.py all               # 三份全跑

    加 --dry-run 仅检查配置，不调 API
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ── 文档配置 ──────────────────────────────────────────────────────────────────

DOC_CONFIGS = {
    "taikelong": {
        "pdf":      REPO / "docs" / "test" / "泰科龙投标文件.pdf",
        "golden":   REPO / "docs" / "test" / "泰科龙投标清单.xlsx",
        "declared": 1_067_616.41,
        "expected": 89,
        # golden column indices (0-based)
        "col_seq":        0,
        "col_name":       2,   # 项目名称
        "col_spec":       3,
        "col_model":      4,
        "col_unit":      11,
        "col_qty":       12,
        "col_price_excl": 13,  # 单价(不含税)
        "col_total_excl": 14,  # 合价(不含税)
        "col_tax_rate":  15,
        "col_price_incl": 17,  # 含税单价  (col 17 based on header pos)
        "col_total_incl": 17,  # 含税合价  (taikelong col17=含税合价)
        "col_brand":     18,
    },
    "kaishuo": {
        "pdf":      REPO / "docs" / "test" / "凯硕新正投标文件.pdf",
        "golden":   REPO / "docs" / "test" / "凯硕新正投标清单.xlsx",
        "declared": 932_154.0,
        "expected": 89,
        "col_seq":        0,
        "col_name":       2,
        "col_spec":       3,
        "col_model":      4,
        "col_unit":      11,
        "col_qty":       12,
        "col_price_excl": 13,
        "col_total_excl": 14,
        "col_tax_rate":  15,
        "col_price_incl": 17,  # 含税单价
        "col_total_incl": 18,  # 含税合价
        "col_brand":     19,
    },
    "miancun": {
        "pdf":      REPO / "docs" / "test" / "上海绵存投标文件.pdf",
        "golden":   REPO / "docs" / "test" / "上海绵存投标清单.xlsx",
        "declared": 1_667_051.0,
        "expected": 89,
        "col_seq":   0,
        "col_name":  1,   # 品类
        "col_spec":  2,
        "col_model": 3,
        "col_unit":  4,
        "col_qty":   5,
        "col_price_excl": None,  # 绵存无不含税列
        "col_total_excl": None,
        "col_tax_rate":   None,
        "col_price_incl": 6,     # 单价
        "col_total_incl": 7,     # 合价
        "col_brand":      None,
    },
}


# ── 日志捕获 ──────────────────────────────────────────────────────────────────

class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def lines(self) -> list[str]:
        fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        return [fmt.format(r) for r in self.records]

    def search(self, pattern: str) -> list[str]:
        return [self.format_record(r) for r in self.records if pattern in r.getMessage()]

    def format_record(self, r: logging.LogRecord) -> str:
        fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
        return fmt.format(r)

    def search_re(self, pattern: str) -> list[re.Match]:
        matches = []
        for r in self.records:
            m = re.search(pattern, r.getMessage())
            if m:
                matches.append(m)
        return matches


def _setup_logging() -> _LogCapture:
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)

    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    h.setLevel(logging.INFO)
    root.addHandler(h)
    return cap


# ── Golden 加载 ───────────────────────────────────────────────────────────────

def _load_golden(cfg: dict) -> list[dict]:
    """Load golden rows from Excel. Returns list of {seq, name, spec, model, unit, qty,
    price_excl, total_excl, tax_rate, price_incl, total_incl, brand}."""
    try:
        import openpyxl
    except ImportError:
        print("[golden] openpyxl not installed, skipping golden load")
        return []

    wb = openpyxl.load_workbook(str(cfg["golden"]))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    golden_rows = []
    for r in rows[1:]:  # skip header
        if r[cfg["col_seq"]] is None:
            continue
        seq_v = str(r[cfg["col_seq"]]).strip()
        if not seq_v.isdigit():
            continue  # skip total row

        def _g(col_key: str):
            idx = cfg.get(col_key)
            if idx is None or idx >= len(r):
                return None
            v = r[idx]
            if v is None:
                return None
            try:
                return float(v) if isinstance(v, (int, float)) else str(v).strip() or None
            except Exception:
                return str(v).strip() or None

        golden_rows.append({
            "seq":        int(seq_v),
            "name":       _g("col_name"),
            "spec":       _g("col_spec"),
            "model":      _g("col_model"),
            "unit":       _g("col_unit"),
            "qty":        _g("col_qty"),
            "price_excl": _g("col_price_excl"),
            "total_excl": _g("col_total_excl"),
            "tax_rate":   _g("col_tax_rate"),
            "price_incl": _g("col_price_incl"),
            "total_incl": _g("col_total_incl"),
            "brand":      _g("col_brand"),
        })

    return golden_rows


# ── ExtractionDraft → JSON ────────────────────────────────────────────────────

def _draft_to_dict(draft) -> dict:
    """Serialize ExtractionDraft to plain dict."""
    def _row_to_dict(r) -> dict:
        sr = r.source_ref
        return {
            "row_index":       r.row_index,
            "row_type":        r.row_type,
            "fields":          r.fields,
            "source_ref": {
                "page":  sr.page if sr else 0,
                "table": sr.table if sr else 0,
                "row":   sr.row if sr else 0,
                "bbox":  list(sr.bbox) if sr and sr.bbox else None,
            } if sr else None,
            "validation_flags": r.validation_flags,
            "field_sources":    r.field_sources,
        }

    return {
        "doc_type":             draft.doc_type,
        "source_file":          str(draft.source_file),
        "page_count":           draft.page_count,
        "processed_page_count": draft.processed_page_count,
        "target_pages":         draft.target_pages,
        "quality":              draft.quality.to_dict(),
        "meta":                 draft.meta,
        "rows":                 [_row_to_dict(r) for r in draft.rows],
        "review_candidates":    [_row_to_dict(r) for r in (draft.review_candidates or [])],
    }


# ── Log 解析 ─────────────────────────────────────────────────────────────────

def _parse_rotation_stats(cap: _LogCapture) -> dict:
    """解析旋转相关日志，支持当前 chain orientation 格式。"""
    stats = {
        "chain_detections":   [],   # [(chain_start, chain_end, scores, sample, chosen_deg)]
        "corrected_pages":    {},   # page → deg
        "recall_pages_rot":   {},   # page → deg (recall)
        "failed_pages":       [],   # pages where rotation NOT applied
        "probe_cache_hits":   [],   # pages that reused probe cache
        "visual_orientation": {},   # page → deg (from visual classification)
    }

    # chain orient scores line: "chain 4-14 orient scores={0: 10, 90: 12, 270: 6} sample=[4, 5] -> 90°"
    for m in cap.search_re(r"chain (\d+)-(\d+) orient scores=(\{[^}]+\}) sample=(\[[^\]]*\]) -> (\d+)"):
        try:
            stats["chain_detections"].append({
                "start":  int(m.group(1)),
                "end":    int(m.group(2)),
                "scores": json.loads(m.group(3).replace("'", '"')),
                "sample": json.loads(m.group(4)),
                "chosen": int(m.group(5)),
            })
        except Exception:
            pass

    # correction: "  p6 corrected → 90° (chain direct)"
    for m in cap.search_re(r"p(\d+) corrected .* (\d+)° \(chain direct\)"):
        stats["corrected_pages"][int(m.group(1))] = int(m.group(2))

    # probe cache hit: "  p4 reuse probe OCR → 90° (no re-OCR)"
    for m in cap.search_re(r"p(\d+) reuse probe OCR .* (\d+)°"):
        stats["probe_cache_hits"].append(int(m.group(1)))
        stats["corrected_pages"][int(m.group(1))] = int(m.group(2))

    # recall page correction: "  recall p15 direct → 90° (chain-inherited, no re-vote)"
    for m in cap.search_re(r"recall p(\d+) direct .* (\d+)°"):
        stats["recall_pages_rot"][int(m.group(1))] = int(m.group(2))

    # failed rotation: "  p6 rotation NOT applied"
    for m in cap.search_re(r"p(\d+) rotation NOT applied"):
        stats["failed_pages"].append(int(m.group(1)))

    # visual orientation from log: "recognize_tables[...]: file=... total=53 target=[...] rotated={...}"
    for m in cap.search_re(r"recognize_tables\[.*\].*rotated=(\{[^}]*\})"):
        try:
            stats["visual_orientation"] = {
                int(k): int(v)
                for k, v in json.loads(m.group(1).replace("'", '"')).items()
            }
        except Exception:
            pass

    return stats


# ── Row diff vs golden ────────────────────────────────────────────────────────

def _diff_vs_golden(extracted_rows: list, golden_rows: list, cfg: dict) -> dict:
    """逐行对比 extracted vs golden，输出 extra / missing / matched。"""
    golden_by_seq = {r["seq"]: r for r in golden_rows}

    # collected extracted quote_lines
    ext_by_seq: dict[int, list] = {}  # seq → list of extracted rows (can be multiple if dup)
    no_seq_rows = []
    for r in extracted_rows:
        if r.row_type != "quote_line":
            continue
        seq_s = str(r.fields.get("seq") or "").strip()
        if seq_s.isdigit():
            seq = int(seq_s)
            ext_by_seq.setdefault(seq, []).append(r)
        else:
            no_seq_rows.append(r)

    # Compare
    matched = []
    missing = []  # seqs in golden but not extracted
    extra_seqs = []  # seqs in extracted but not in golden

    # 位置对齐兜底：若全部抽取行无 seq（PDF 无序号列），按排序顺序与 golden 位置对齐
    if not ext_by_seq and no_seq_rows:
        sorted_no_seq = sorted(
            no_seq_rows,
            key=lambda r: (r.source_ref.page if r.source_ref else 0,
                           r.source_ref.row if r.source_ref else 0),
        )
        golden_sorted = sorted(golden_rows, key=lambda g: g["seq"])
        for i, (e, g) in enumerate(zip(sorted_no_seq, golden_sorted)):
            f = e.fields
            matched.append({
                "seq":           g["seq"],
                "name_ok":       _approx_eq_str(f.get("name"), g.get("name")),
                "qty_ok":        _approx_eq_num(f.get("qty"), g.get("qty")),
                "total_incl_ok": _approx_eq_num(
                    f.get("total_price_incl_tax") or f.get("total_price"),
                    g.get("total_incl"),
                    rel_tol=0.01,
                ),
                "ext_total_incl":    f.get("total_price_incl_tax") or f.get("total_price"),
                "golden_total_incl": g.get("total_incl"),
                "ext_name":    f.get("name"),
                "golden_name": g.get("name"),
                "ext_qty":     f.get("qty"),
                "golden_qty":  g.get("qty"),
                "page":        e.source_ref.page if e.source_ref else 0,
                "position_matched": True,
            })
        # remaining golden rows without a match
        for g in golden_sorted[len(sorted_no_seq):]:
            missing.append(g["seq"])
        return {
            "total_extracted":  len(no_seq_rows),
            "total_golden":     len(golden_rows),
            "matched":          matched,
            "missing_seqs":     missing,
            "extra_seqs":       extra_seqs,
            "no_seq_extracted": len(no_seq_rows),
            "no_seq_rows": [],
            "position_matched": True,
        }

    all_seqs = sorted(set(list(golden_by_seq.keys()) + list(ext_by_seq.keys())))
    for seq in all_seqs:
        g = golden_by_seq.get(seq)
        e_list = ext_by_seq.get(seq, [])
        if g and e_list:
            e = e_list[0]  # take first
            f = e.fields
            matched.append({
                "seq":          seq,
                "name_ok":      _approx_eq_str(f.get("name"), g.get("name")),
                "qty_ok":       _approx_eq_num(f.get("qty"), g.get("qty")),
                "total_incl_ok": _approx_eq_num(
                    f.get("total_price_incl_tax") or f.get("total_price"),
                    g.get("total_incl"),
                    rel_tol=0.01,
                ),
                "ext_total_incl": f.get("total_price_incl_tax") or f.get("total_price"),
                "golden_total_incl": g.get("total_incl"),
                "ext_name":    f.get("name"),
                "golden_name": g.get("name"),
                "ext_qty":     f.get("qty"),
                "golden_qty":  g.get("qty"),
                "page":        e.source_ref.page if e.source_ref else 0,
            })
            if len(e_list) > 1:
                extra_seqs.append({"seq": seq, "count": len(e_list), "reason": "duplicate_seq"})
        elif g and not e_list:
            missing.append(seq)
        elif e_list and not g:
            e = e_list[0]
            f = e.fields
            extra_seqs.append({
                "seq":    seq,
                "name":   f.get("name"),
                "spec":   f.get("spec"),
                "qty":    f.get("qty"),
                "total":  f.get("total_price_incl_tax") or f.get("total_price"),
                "page":   e.source_ref.page if e.source_ref else 0,
                "reason": "not_in_golden",
            })

    return {
        "total_extracted":  sum(1 for r in extracted_rows if r.row_type == "quote_line"),
        "total_golden":     len(golden_rows),
        "matched":          matched,
        "missing_seqs":     missing,
        "extra_seqs":       extra_seqs,
        "no_seq_extracted": len(no_seq_rows),
        "no_seq_rows": [
            {"name": r.fields.get("name"), "qty": r.fields.get("qty"),
             "total": r.fields.get("total_price_incl_tax") or r.fields.get("total_price"),
             "page": r.source_ref.page if r.source_ref else 0}
            for r in no_seq_rows
        ],
    }


def _approx_eq_str(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def _approx_eq_num(a, b, rel_tol: float = 0.001) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
        if fb == 0:
            return fa == 0
        return abs(fa - fb) / max(abs(fb), 1e-9) <= rel_tol
    except (TypeError, ValueError):
        return False


# ── Summary stats ─────────────────────────────────────────────────────────────

def _compute_total_incl(rows, field_preference=("total_price_incl_tax", "total_price")):
    total = 0.0
    for r in rows:
        if r.row_type != "quote_line":
            continue
        for field in field_preference:
            v = r.fields.get(field)
            if v is not None:
                try:
                    total += float(v)
                    break
                except (TypeError, ValueError):
                    pass
    return total


# ── Main run for one doc ──────────────────────────────────────────────────────

def run_one(doc_name: str, out_dir: Path, dry_run: bool = False) -> dict:
    """Run fresh E2E for one document. Returns result dict."""
    cfg = DOC_CONFIGS[doc_name]
    pdf_path = cfg["pdf"]
    golden_path = cfg["golden"]

    print(f"\n{'='*70}")
    print(f"[{doc_name.upper()}] Fresh E2E")
    print(f"PDF:    {pdf_path.name}")
    print(f"Golden: {golden_path.name}")
    print(f"Output: {out_dir}")
    print(f"期望行数: {cfg['expected']}   声明总额: {cfg['declared']:,.2f}")
    print(f"{'='*70}")

    if not pdf_path.exists():
        return {"error": f"PDF not found: {pdf_path}", "pass": False}
    if not golden_path.exists():
        return {"error": f"Golden not found: {golden_path}", "pass": False}

    if dry_run:
        print("[DRY RUN] Skipping API calls.")
        return {"dry_run": True, "pass": None}

    # Setup output dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Setup logging
    cap = _setup_logging()

    # Load golden
    golden_rows = _load_golden(cfg)
    print(f"Golden loaded: {len(golden_rows)} rows")

    # Build provider
    from apps.api.core.config import get_settings
    s = get_settings()
    if not s.DASHSCOPE_API_KEY:
        return {"error": "DASHSCOPE_API_KEY not configured", "pass": False}

    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.intelligence.snapshot_provider import SnapshotProvider

    real_prov = DashScopeOCRProvider(
        api_key=s.DASHSCOPE_API_KEY,
        base_url=s.DASHSCOPE_BASE_URL,
        ocr_model=s.DASHSCOPE_OCR_MODEL,
        llm_model=s.DASHSCOPE_LLM_MODEL,
    )

    # Use fresh temp snapshot in output dir (never deleted)
    snap_path = out_dir / "snapshot.json"
    provider = SnapshotProvider(real_prov, snap_path, mode="record")

    # Run recognition
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter
    adapter = _get_quote_adapter()

    print("\n[1/4] 运行 recognize_tables（全 fresh）...")
    t0 = time.time()
    try:
        draft = recognize_tables(str(pdf_path), provider, adapter)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        (out_dir / "error.txt").write_text(err, encoding="utf-8")
        return {"error": str(e), "pass": False}
    elapsed = time.time() - t0
    print(f"[1/4] 完成，耗时 {elapsed:.1f}s")

    # Save full draft JSON
    print("\n[2/4] 保存诊断资产...")
    draft_dict = _draft_to_dict(draft)
    (out_dir / "draft.json").write_text(
        json.dumps(draft_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Save per-page summary
    per_page = []
    for r in draft.rows:
        sr = r.source_ref
        per_page.append({
            "seq":          r.fields.get("seq"),
            "name":         r.fields.get("name") or r.fields.get("material"),
            "row_type":     r.row_type,
            "page":         sr.page if sr else 0,
            "table":        sr.table if sr else 0,
            "row":          sr.row if sr else 0,
            "parser_mode":  r.fields.get("parser_mode"),
            "qty":          r.fields.get("qty"),
            "total_incl":   r.fields.get("total_price_incl_tax") or r.fields.get("total_price"),
            "total_excl":   r.fields.get("total_price_excl_tax"),
            "validation_flags": r.validation_flags,
        })
    (out_dir / "per_row.json").write_text(
        json.dumps(per_page, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Save log
    (out_dir / "full.log").write_text(
        "\n".join(cap.lines()),
        encoding="utf-8",
    )

    # Save rotation stats + snapshot
    rot_stats = _parse_rotation_stats(cap)
    snap_stats = provider.stats
    (out_dir / "rotation_stats.json").write_text(
        json.dumps(rot_stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        provider.save()
    except Exception as _e:
        print(f"  [WARN] snapshot.json save failed: {_e}")

    print(f"  Saved: draft.json, per_row.json, full.log, rotation_stats.json, snapshot.json")

    # Golden diff
    print("\n[3/4] Golden diff...")
    diff = _diff_vs_golden(draft.rows, golden_rows, cfg)
    (out_dir / "golden_diff.json").write_text(
        json.dumps(diff, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Compute totals
    ext_total = _compute_total_incl(draft.rows)
    declared = cfg["declared"]
    total_gap = ext_total - declared

    # Print summary
    print(f"\n[4/4] 结果汇总:")
    print(f"  API 调用: visual={snap_stats['visual_misses']} OCR={snap_stats['ocr_misses']} LLM={snap_stats['llm_misses']}")
    print(f"  质量等级: {draft.quality.status}")
    print(f"  行数: 抽取={diff['total_extracted']} / golden={diff['total_golden']}")
    print(f"  匹配行: {len(diff['matched'])}")
    print(f"  missing seqs: {diff['missing_seqs']}")
    print(f"  extra seqs:   {[x['seq'] for x in diff['extra_seqs']]}")
    print(f"  无seq行:      {diff['no_seq_extracted']}")
    print(f"  含税总额: 抽取={ext_total:,.2f} / 声明={declared:,.2f} / 差={total_gap:+,.2f}")

    # Rotation stats
    if rot_stats["chain_detections"]:
        for det in rot_stats["chain_detections"]:
            print(f"  链方向: chain {det['start']}-{det['end']} scores={det['scores']} → {det['chosen']}°")
    print(f"  旋转修正页: {sorted(rot_stats['corrected_pages'].keys())}")
    print(f"  探测缓存命中: {rot_stats['probe_cache_hits']}")
    if rot_stats["failed_pages"]:
        print(f"  旋转失败页: {rot_stats['failed_pages']}")

    # Extra row detail
    if diff["extra_seqs"]:
        print(f"\n  [EXTRA ROWS DETAIL]")
        for x in diff["extra_seqs"]:
            print(f"    seq={x.get('seq')} page={x.get('page')} name={x.get('name')} "
                  f"spec={x.get('spec')} qty={x.get('qty')} total={x.get('total')} "
                  f"reason={x.get('reason')}")

    if diff["missing_seqs"]:
        print(f"\n  [MISSING ROWS DETAIL]")
        for seq in diff["missing_seqs"]:
            g = {r["seq"]: r for r in golden_rows}.get(seq, {})
            print(f"    seq={seq} name={g.get('name')} total_incl={g.get('total_incl')}")

    # Verdict
    row_ok = diff["total_extracted"] == cfg["expected"]
    missing_ok = len(diff["missing_seqs"]) == 0
    extra_ok = len(diff["extra_seqs"]) == 0
    total_ok = abs(total_gap) / max(declared, 1) < 0.001
    quality_ok = draft.quality.status in ("PASS", "REVIEW")

    print(f"\n{'='*70}")
    print("结论:")
    print(f"  行数:    {'OK' if row_ok else 'FAIL'} ({diff['total_extracted']}/{cfg['expected']})")
    print(f"  missing: {'OK' if missing_ok else 'FAIL'} {diff['missing_seqs']}")
    print(f"  extra:   {'OK' if extra_ok else 'FAIL'} {[x['seq'] for x in diff['extra_seqs']]}")
    print(f"  总额:    {'OK' if total_ok else 'FAIL'} 差={total_gap:+,.2f}")
    print(f"  质量:    {'OK' if quality_ok else 'FAIL'} {draft.quality.status}")

    passed = row_ok and missing_ok and extra_ok and total_ok and quality_ok
    print(f"\n  总体: {'PASS [OK]' if passed else 'FAIL [FAIL]'}")
    print(f"{'='*70}")

    result = {
        "doc":             doc_name,
        "elapsed_s":       round(elapsed, 1),
        "api_calls":       snap_stats,
        "quality":         draft.quality.status,
        "row_count":       diff["total_extracted"],
        "expected":        cfg["expected"],
        "matched":         len(diff["matched"]),
        "missing":         diff["missing_seqs"],
        "extra":           diff["extra_seqs"],
        "no_seq_rows":     diff["no_seq_extracted"],
        "total_incl_ext":  ext_total,
        "total_incl_declared": declared,
        "total_gap":       total_gap,
        "rotation":        rot_stats,
        "pass":            passed,
        "out_dir":         str(out_dir),
    }

    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fresh E2E multi-doc verification")
    parser.add_argument("docs", nargs="*", default=["taikelong"],
                        help="Document names: taikelong kaishuo miancun all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default=None,
                        help="Override output base directory (default: tmp/)")
    args = parser.parse_args()

    docs_to_run = []
    for d in args.docs:
        if d == "all":
            docs_to_run.extend(DOC_CONFIGS.keys())
        elif d in DOC_CONFIGS:
            docs_to_run.append(d)
        else:
            print(f"Unknown doc: {d}. Valid: {list(DOC_CONFIGS.keys())} all")
            sys.exit(1)

    # Deduplicate while preserving order
    seen_docs = []
    for d in docs_to_run:
        if d not in seen_docs:
            seen_docs.append(d)
    docs_to_run = seen_docs

    base_out = Path(args.out) if args.out else REPO / "tmp"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    for doc in docs_to_run:
        out_dir = base_out / f"fresh_e2e_{doc}_{ts}"
        result = run_one(doc, out_dir, dry_run=args.dry_run)
        results.append(result)

    # Final summary
    if len(results) > 1:
        print(f"\n{'='*70}")
        print("总体汇总:")
        for r in results:
            status = "PASS ✓" if r.get("pass") else "FAIL ✗"
            print(f"  {r.get('doc', '?'):12s} {status}  行数={r.get('row_count','?')}/{r.get('expected','?')}")
        print(f"{'='*70}")

    all_pass = all(r.get("pass") for r in results)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
