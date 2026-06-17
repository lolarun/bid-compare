"""Excel 清单 vs PDF 投标清单对账。

两种口径由 source_type 控制：

  "excel_primary"（默认）：Excel 是锚点主来源，PDF 是品牌/材质补充。
      差异时 recommended_source='excel'，前端须人工确认后才能继续。

  "pdf_primary"：PDF 招标清单是比价主来源，Excel 仅作参考对照。
      recommended_source 始终为 'pdf'，不阻断流程。
      Excel 独有行 → only_in_excel_reference（不进入主清单）。
      差异只作提示，无需人工确认。
"""

from __future__ import annotations


def _norm_qty(v) -> float | None:
    """数量归一化为 float，忽略空/0。"""
    if v is None or v == "" or v == 0:
        return None
    try:
        return float(str(v).replace(",", "").replace("，", ""))
    except (ValueError, TypeError):
        return None


def _norm_str(v) -> str:
    return str(v or "").strip()


# 对比字段 → 中文标签
_COMPARE_FIELDS = [
    ("name",  "品名"),
    ("spec",  "规格"),
    ("unit",  "单位"),
    ("qty",   "数量"),
]


def reconcile_anchors(
    xlsx_items: list[dict],
    pdf_items: list[dict],
    source_type: str = "excel_primary",
) -> dict:
    """逐序号对账 Excel 锚点 vs PDF 锚点，返回差异报告。

    Args:
        xlsx_items:  TenderPreviewItem JSON list（来自 /tender-list/preview）。
        pdf_items:   TenderBidlistResult.items JSON list（来自 OCR 抽取结果）。
        source_type: "excel_primary"（默认）| "pdf_primary"

    Returns (excel_primary):
        { xlsx_count, pdf_count,
          seq_missing_in_pdf, seq_missing_in_xlsx, field_mismatches,
          recommended_source: 'both_consistent' | 'excel' }

    Returns (pdf_primary):
        { xlsx_count, pdf_count,
          only_in_excel_reference,    # Excel 独有行，不进入 PDF 主清单
          seq_missing_in_pdf,         # 同上（向后兼容别名）
          seq_missing_in_xlsx,        # PDF 独有行（正常进入主清单）
          field_mismatches,           # 同序号字段差异（仅提示）
          recommended_source: 'pdf' }
    """
    def _seq(it: dict) -> str:
        return _norm_str(it.get("seq", ""))

    xlsx_by_seq = {_seq(it): it for it in xlsx_items if _seq(it)}
    pdf_by_seq  = {_seq(it): it for it in pdf_items  if _seq(it)}

    def _sort_key(s: str) -> tuple:
        return (int(s),) if s.isdigit() else (10**9, s)

    seq_missing_in_pdf  = sorted([s for s in xlsx_by_seq if s not in pdf_by_seq],  key=_sort_key)
    seq_missing_in_xlsx = sorted([s for s in pdf_by_seq  if s not in xlsx_by_seq], key=_sort_key)

    field_mismatches: list[dict] = []
    for seq in sorted(xlsx_by_seq.keys() & pdf_by_seq.keys(), key=_sort_key):
        x = xlsx_by_seq[seq]
        p = pdf_by_seq[seq]
        for field, label in _COMPARE_FIELDS:
            xv = _norm_str(x.get(field))
            pv = _norm_str(p.get(field))
            if not xv or not pv:
                continue  # 一方为空 → 无法对比，不标差异
            if field == "qty":
                xf, pf = _norm_qty(xv), _norm_qty(pv)
                if xf is None or pf is None:
                    continue
                if abs(xf - pf) < 0.01:
                    continue
            elif xv == pv:
                continue
            field_mismatches.append({
                "seq": seq,
                "field": label,
                "xlsx_value": xv,
                "pdf_value": pv,
            })

    if source_type == "pdf_primary":
        return {
            "xlsx_count": len(xlsx_items),
            "pdf_count":  len(pdf_items),
            "only_in_excel_reference": seq_missing_in_pdf,  # Excel 独有，参考用
            "seq_missing_in_pdf":      seq_missing_in_pdf,  # compat alias
            "seq_missing_in_xlsx":     seq_missing_in_xlsx,
            "field_mismatches":        field_mismatches,
            "recommended_source":      "pdf",
        }

    is_consistent = (
        not seq_missing_in_pdf
        and not seq_missing_in_xlsx
        and not field_mismatches
    )
    return {
        "xlsx_count": len(xlsx_items),
        "pdf_count":  len(pdf_items),
        "seq_missing_in_pdf":  seq_missing_in_pdf,
        "seq_missing_in_xlsx": seq_missing_in_xlsx,
        "field_mismatches":    field_mismatches,
        "recommended_source":  "both_consistent" if is_consistent else "excel",
    }
