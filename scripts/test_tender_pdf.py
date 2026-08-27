"""一次性脚本：对真实招标 PDF 跑 tender_pdf.extract_bidlist，输出完整诊断报告。

用法：
    python scripts/test_tender_pdf.py [pdf_path [xlsx_path]]

pdf_path  默认: tests/fixtures/documents/金桥地体上盖项目-招标文件.pdf
xlsx_path 默认: tests/fixtures/documents/金桥地体上盖项目-采购清单.xlsx

输出：
  - 完整诊断报告（标准输出）
  - _tender_pdf_result.json（完整 JSON 结果，供详查）
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
PDF  = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "tests/fixtures/documents/金桥地体上盖项目-招标文件.pdf")
XLSX = sys.argv[2] if len(sys.argv) > 2 else str(
    REPO / "tests/fixtures/documents/金桥地体上盖项目-采购清单.xlsx"
)


def _pct(rate: float) -> str:
    return f"{rate:.1%}"


def main() -> None:
    from apps.api.core.config import get_settings
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.services.tender.tender_pdf import extract_bidlist

    s = get_settings()
    if not s.DASHSCOPE_API_KEY:
        print("ERROR: DASHSCOPE_API_KEY 未配置")
        sys.exit(1)

    provider = DashScopeOCRProvider(
        api_key=s.DASHSCOPE_API_KEY,
        base_url=s.DASHSCOPE_BASE_URL,
        ocr_model=s.DASHSCOPE_OCR_MODEL,
        llm_model=s.DASHSCOPE_LLM_MODEL,
    )

    xlsx_arg = XLSX if Path(XLSX).exists() else None
    if not xlsx_arg:
        print(f"[警告] Excel 不存在，跳过对账: {XLSX}")

    def prog(stage: str, pct: int) -> None:
        print(f"  [{pct:3d}%] {stage}")

    print(f"\n{'═'*60}")
    print(f"extract_bidlist: {PDF}")
    print(f"{'═'*60}")
    result = extract_bidlist(PDF, provider, progress_cb=prog, xlsx_path=xlsx_arg)

    qm = result["quality_metrics"]
    recon = result.get("reconcile") or {}

    # ── 1. 页定位 ────────────────────────────────────────────────────
    print(f"\n[页定位]")
    print(f"  brand_page    : {result['detected_pages']['brand']}")
    print(f"  bidlist_pages : {result['detected_pages']['bidlist']}")
    print(f"  source_type   : {result['source_type']}")

    # ── 2. TableGrid 使用率 ───────────────────────────────────────────
    print(f"\n[TableGrid 使用率]")
    tg = qm["table_grid_pages"]
    fb = qm["html_fallback_pages"]
    total_pages = len(result["page_diagnostics"])
    tg_rate = len(tg) / total_pages if total_pages else 0
    print(f"  table_grid     : {tg}  ({_pct(tg_rate)})")
    print(f"  html_fallback  : {[f['page'] for f in fb]}")
    print()
    for d in result["page_diagnostics"]:
        retry_tag = " ← thinking-retry" if d["thinking_retry"] else ""
        reason_tag = f"  [reason={d['fallback_reason']}]" if d["fallback_reason"] else ""
        print(
            f"  page {d['page']:>2}: {d['input_mode']:<12}  "
            f"expected={d['expected_rows']:>3}  extracted={d['extracted_rows']:>3}"
            f"{reason_tag}{retry_tag}"
        )

    # ── 3. 行数 & seq 范围 ────────────────────────────────────────────
    print(f"\n[行数 & seq]")
    all_seqs = [str(it.get("seq", "")).strip() for it in result["items"] if it.get("seq")]
    numeric_seqs = sorted(int(s) for s in all_seqs if s.isdigit())
    seq_range = f"{numeric_seqs[0]}..{numeric_seqs[-1]}" if numeric_seqs else "n/a"
    print(f"  row_count      : {result['row_count']}")
    print(f"  seq range      : {seq_range}")
    print(f"  seq_missing    : {qm['seq_missing'] or '(none)'}")
    print(f"  seq_duplicate  : {qm['seq_duplicate'] or '(none)'}")
    print(f"  by_page        : {qm['row_count_by_page']}")

    # ── 4. 字段覆盖率 ────────────────────────────────────────────────
    print(f"\n[字段覆盖率]")
    print(f"  material_columns : {_pct(qm['material_columns_filled_rate'])}")
    print(f"  brand            : {_pct(qm['brand_filled_rate'])}")
    print(f"  source_ref       : {_pct(qm['source_ref_coverage'])}")
    print(f"  qty_parse        : {_pct(qm['qty_parse_success_rate'])}")

    # ── 5. 品牌表 ────────────────────────────────────────────────────
    print(f"\n[品牌表]")
    print(f"  material_class   : {result['material_class']}")
    for b in result["brand_requirement"]:
        print(f"  品牌要求: {b['brand_en']} / {b['brand_cn']}")
    for sb in result["supplier_brands"]:
        print(f"  供应商品牌: {sb['supplier_name'][:12]}  → {sb['brand']}")

    # ── 6. Excel vs PDF 对账 ─────────────────────────────────────────
    print(f"\n[Excel vs PDF 对账]")
    if recon and "error" not in recon:
        print(f"  xlsx_count         : {recon['xlsx_count']}")
        print(f"  pdf_count          : {recon['pdf_count']}")
        print(f"  missing_in_pdf     : {recon['seq_missing_in_pdf'] or '(none)'}")
        print(f"  missing_in_xlsx    : {recon['seq_missing_in_xlsx'] or '(none)'}")
        print(f"  field_mismatches   : {len(recon['field_mismatches'])} 处")
        print(f"  recommended_source : {recon['recommended_source']}")
        if recon["field_mismatches"]:
            print()
            for m in recon["field_mismatches"][:15]:
                print(f"    seq={m['seq']:>3} {m['field']:6}: Excel={m['xlsx_value']!r:<20} PDF={m['pdf_value']!r}")
        if recon["recommended_source"] != "both_consistent":
            print("\n  [!] PDF 与 Excel 存在差异，不能静默以 PDF 替代 Excel！请人工确认后再确认清单。")
    elif recon and "error" in recon:
        print(f"  对账异常: {recon['error']}")
    else:
        print("  (未传入 Excel，跳过对账)")

    # ── 7. 前6行样例 ─────────────────────────────────────────────────
    items = result["items"]
    print(f"\n[前6行样例]")
    for it in items[:6]:
        mat_str = "|".join(f"{k}:{v}" for k, v in (it.get("materials") or {}).items()) or "(无材质)"
        print(f"  seq={it['seq']:>3} {it['name']:<12} {it.get('spec',''):<8}  {mat_str}")

    print(f"\n{'═'*60}")

    # 落盘
    import os
    os.makedirs(str(REPO / "outputs"), exist_ok=True)
    out_path = str(REPO / "outputs" / "tender_pdf_report_jingqiao.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"完整结果已写入 {out_path}")


if __name__ == "__main__":
    main()
