"""fresh_taikelong_e2e.py — 泰科龙完整 fresh E2E 验证脚本。

使用独立临时快照（空快照 record 模式），全程 fresh：
  视觉分类 / 0° OCR / 旋转探测OCR / 旋转后OCR / 字段LLM

报告：
  - 视觉方向（每页 orientation）
  - q0 分数（按 rotation-fallback 日志）
  - suspects 页面
  - 90°/270° 探测票数
  - 最终旋转页面
  - 各阶段 API 调用数
  - 89行召回率
  - 全部报价行精确率
  - 含税总额及与 1,067,616.41 的差额

禁止：
  - 覆盖或读取官方 fixture（tests/fixtures/ocr_snapshots/*.json）
  - 任何旧 visual/OCR/LLM 缓存命中（全部走 fresh API）

用法：
    python scripts/fresh_taikelong_e2e.py
"""
from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PDF_PATH = REPO / "docs" / "test" / "泰科龙投标文件.pdf"
XLSX_GOLDEN = REPO / "docs" / "test" / "泰科龙投标清单.xlsx"
DECLARED_TOTAL = 1_067_616.41
EXPECTED_ROWS = 89


# ── 日志捕获 ─────────────────────────────────────────────────────────────────

class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[tuple[str, str]] = []  # (logger_name, message)

    def emit(self, record):
        self.lines.append((record.name, self.format(record)))


def _setup_logging():
    cap = _LogCapture()
    cap.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(cap)
    root.setLevel(logging.INFO)

    # Also print to stderr so we can see progress
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    return cap


# ── Golden 加载（识别结束后才用）────────────────────────────────────────────

def _load_golden_totals() -> tuple[list[float], float]:
    """Return (list of incl_tax totals per line, declared_total)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(XLSX_GOLDEN))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        # Col index 17 (0-based) = R = 含税合价; last row is totals row
        totals = []
        for r in rows[1:]:                   # skip header
            if r[0] is None:                 # seq is None → total/summary row
                continue
            v = r[17]
            if v is not None:
                try:
                    totals.append(float(v))
                except (TypeError, ValueError):
                    pass
        return totals, DECLARED_TOTAL
    except Exception as e:
        print(f"[golden] load failed: {e}")
        return [], DECLARED_TOTAL


# ── 主流程 ─────────────────────────────────────────────────────────────────

def main():
    cap = _setup_logging()

    print(f"\n{'='*70}")
    print("泰科龙 完整 Fresh E2E 验证")
    print(f"PDF: {PDF_PATH.name}")
    print(f"期望行数: {EXPECTED_ROWS}   声明含税总额: {DECLARED_TOTAL:,.2f}")
    print(f"{'='*70}\n")

    if not PDF_PATH.exists():
        sys.exit(f"PDF 未找到: {PDF_PATH}")

    # Build real provider
    from apps.api.core.config import get_settings
    s = get_settings()
    if not s.DASHSCOPE_API_KEY:
        sys.exit("DASHSCOPE_API_KEY 未配置")
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    real_prov = DashScopeOCRProvider(
        api_key=s.DASHSCOPE_API_KEY, base_url=s.DASHSCOPE_BASE_URL,
        ocr_model=s.DASHSCOPE_OCR_MODEL, llm_model=s.DASHSCOPE_LLM_MODEL,
    )

    # Use a truly empty temp snapshot (not the official fixture)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        snap_path = Path(tf.name)
    print(f"临时快照路径: {snap_path}")

    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    provider = SnapshotProvider(real_prov, snap_path, mode="record")

    # Build adapter
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter
    adapter = _get_quote_adapter()

    # Run full pipeline
    print("\n[1/3] 运行 recognize_tables（全 fresh）...\n")
    import time
    t0 = time.time()
    try:
        draft = recognize_tables(str(PDF_PATH), provider, adapter)
    except Exception as e:
        print(f"\n[FAIL] recognize_tables raised: {e}")
        import traceback
        traceback.print_exc()
        return
    elapsed = time.time() - t0
    print(f"\n[1/3] 完成，耗时 {elapsed:.1f}s")

    # ── 收集快照统计 ─────────────────────────────────────────────────────────
    stats = provider.stats
    print(f"\n[2/3] 快照统计（全 miss = fresh）:")
    print(f"  visual API 调用: {stats['visual_misses']}")
    print(f"  OCR   API 调用: {stats['ocr_misses']}")
    print(f"  LLM   API 调用: {stats['llm_misses']}")

    # ── 从日志提取旋转细节 ───────────────────────────────────────────────────
    print("\n[2/3] 旋转兜底日志分析:")

    rotation_log_lines = [
        msg for name, msg in cap.lines
        if "rotation-fallback" in msg or "orientation" in msg.lower()
    ]

    q0_scores: dict[int, int] = {}
    suspects: list[int] = []
    q0_zero_pages: list[int] = []
    probe_votes: dict = {}
    probe_sample: list[int] = []
    candidates: list[int] = []
    corrected_pages: dict[int, int] = {}  # page → deg

    for line in rotation_log_lines:
        # q0 scores: "rotation-fallback q0 scores: {5: 0, 6: 0, ...}"
        m = re.search(r"q0 scores: (\{[^}]+\})", line)
        if m:
            try:
                q0_scores = {int(k): v for k, v in json.loads(m.group(1).replace("'", '"')).items()}
            except Exception:
                pass

        # suspects: "rotation-fallback suspects (0<q<3): [5, 6] (q=0 pages: [7, 8])"
        m = re.search(r"suspects \(0<q<\d+\): (\[[^\]]*\]).*q=0 pages: (\[[^\]]*\])", line)
        if m:
            try:
                suspects = json.loads(m.group(1))
                q0_zero_pages = json.loads(m.group(2))
            except Exception:
                pass

        # probe votes: "rotation-fallback probe votes: {90: 2, 270: 0} (sampled=[5, 8] tested=2)"
        m = re.search(r"probe votes: (\{[^}]+\}) \(sampled=(\[[^\]]*\]) tested=(\d+)\)", line)
        if m:
            try:
                probe_votes = {int(k): v for k, v in json.loads(m.group(1).replace("'", '"')).items()}
                probe_sample = json.loads(m.group(2))
            except Exception:
                pass

        # candidates: "rotation-fallback candidates: [90]"
        m = re.search(r"rotation-fallback candidates: (\[[^\]]*\])", line)
        if m:
            try:
                candidates = json.loads(m.group(1))
            except Exception:
                pass

        # corrected: "Page 5 orientation corrected 90° (q 0→4)" or "applied"
        m = re.search(r"Page (\d+) orientation (corrected|applied) (\d+)°", line)
        if m:
            corrected_pages[int(m.group(1))] = int(m.group(3))

    # Visual orientation from page_cls
    visual_orientation: dict[int, int] = {}
    for name, msg in cap.lines:
        m = re.search(r"recognize_tables\[.*\].*rotated=(\{[^}]*\})", msg)
        if m:
            try:
                visual_orientation = json.loads(m.group(1).replace("'", '"'))
            except Exception:
                pass

    print(f"  视觉分类给出的 orientation≠0: {visual_orientation or '（无，全为0°）'}")
    if q0_scores:
        print(f"  0° OCR q0 分数（目标页）: {q0_scores}")
    else:
        print(f"  0° OCR q0 分数: 未在日志中找到（旋转兜底可能未触发）")
    print(f"  suspects（0<q<{3}）: {suspects}")
    print(f"  q=0 页面（完全无表格结构）: {q0_zero_pages}")
    print(f"  探测样本: {probe_sample}  票数: {probe_votes}")
    print(f"  候选方向: {candidates}")
    print(f"  最终旋转修正页面: {corrected_pages}")

    # ── 行数分析 ─────────────────────────────────────────────────────────────
    print(f"\n[3/3] 抽取结果分析:")
    rows = draft.rows
    quote_lines = [r for r in rows if r.row_type == "quote_line"]
    subtotals = [r for r in rows if r.row_type == "subtotal"]
    grand_totals = [r for r in rows if r.row_type == "grand_total"]
    invalids = [r for r in rows if r.row_type == "invalid"]

    print(f"  总行数: {len(rows)}")
    print(f"  quote_line: {len(quote_lines)}")
    print(f"  subtotal:   {len(subtotals)}")
    print(f"  grand_total:{len(grand_totals)}")
    print(f"  invalid:    {len(invalids)}")

    # 召回率
    recall = len(quote_lines) / EXPECTED_ROWS if EXPECTED_ROWS else 0
    print(f"\n  行数召回: {len(quote_lines)}/{EXPECTED_ROWS} = {recall:.1%}")

    # 精确率（非报价行 / 总行数）
    non_quote = len(subtotals) + len(grand_totals) + len(invalids)
    precision = len(quote_lines) / (len(quote_lines) + non_quote) if rows else 0
    print(f"  精确率（quote_line / 总行）: {precision:.1%}")

    # 含税总额
    incl_tax_total = 0.0
    for r in quote_lines:
        f = r.fields or {}
        v = f.get("total_price_incl_tax") or f.get("total_price") or 0
        try:
            incl_tax_total += float(v)
        except (TypeError, ValueError):
            pass
    gap = incl_tax_total - DECLARED_TOTAL
    pct = gap / DECLARED_TOTAL * 100 if DECLARED_TOTAL else 0
    print(f"\n  识别含税总额: {incl_tax_total:,.2f}")
    print(f"  声明含税总额: {DECLARED_TOTAL:,.2f}")
    print(f"  差额: {gap:+,.2f} ({pct:+.2f}%)")

    # ── 结论 ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("结论判断:")
    if suspects or corrected_pages:
        print("  旋转兜底触发：suspects 非空 或 有页面被修正")
        if corrected_pages:
            print(f"  → 旋转自动成功：修正页 {corrected_pages}")
        else:
            print("  → suspects 存在但候选为空，旋转未修正（探测未通过）")
    else:
        if q0_zero_pages:
            print(f"  suspects=[] 且 q=0 页面={q0_zero_pages}")
            print("  → Claude 指出的 q=0 盲区成立：0°OCR无表格结构，兜底无法触发")
            print("  → 需修复旋转探测入口（使 q=0 页面也能进入候选）")
        else:
            print("  suspects=[]，q0日志未捕获 — 请检查日志输出")

    if len(quote_lines) == EXPECTED_ROWS:
        print(f"  行数: {len(quote_lines)}/{EXPECTED_ROWS} OK")
    else:
        print(f"  行数: {len(quote_lines)}/{EXPECTED_ROWS} FAIL  缺失 {EXPECTED_ROWS - len(quote_lines)} 行")

    if abs(gap) / max(DECLARED_TOTAL, 1) < 0.01:
        print(f"  总额: 差异 {gap:+,.2f} OK (<1%)")
    else:
        print(f"  总额: 差异 {gap:+,.2f} ({pct:+.2f}%) FAIL")

    print(f"{'='*70}")

    # Clean up temp snapshot
    try:
        snap_path.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
