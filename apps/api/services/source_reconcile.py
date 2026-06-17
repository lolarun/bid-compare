"""Excel 清单 vs PDF 投标清单对账。

业务背景：Excel 招标清单（结构化行项目）与 PDF 招标文件投标清单是同一份标书的两种表达，
可能存在行数差异或字段不一致。投产前须对账确认，差异必须经人工确认，不允许静默替换。

默认口径：
- Excel = 结构化行项目主来源（确认的锚点序列）
- PDF  = 品牌/材质/供应商品牌映射补充来源
- 只有没有 Excel 或人工明确选择 PDF 时，PDF 才作为清单主来源
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


def reconcile_anchors(xlsx_items: list[dict], pdf_items: list[dict]) -> dict:
    """逐序号对账 Excel 锚点 vs PDF 锚点，返回差异报告。

    Args:
        xlsx_items: TenderPreviewItem JSON list（来自 /tender-list/preview）。
        pdf_items:  TenderBidlistResult.items JSON list（来自 OCR 抽取结果）。

    Returns:
        {
            xlsx_count, pdf_count,
            seq_missing_in_pdf,    # Excel 有、PDF 没有
            seq_missing_in_xlsx,   # PDF 有、Excel 没有
            field_mismatches: [    # 同序号下字段值不一致
                {seq, field, xlsx_value, pdf_value}
            ],
            recommended_source: 'both_consistent' | 'excel'
        }
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

    is_consistent = (
        not seq_missing_in_pdf
        and not seq_missing_in_xlsx
        and not field_mismatches
    )
    return {
        "xlsx_count": len(xlsx_items),
        "pdf_count": len(pdf_items),
        "seq_missing_in_pdf":  seq_missing_in_pdf,
        "seq_missing_in_xlsx": seq_missing_in_xlsx,
        "field_mismatches": field_mismatches,
        "recommended_source": "both_consistent" if is_consistent else "excel",
    }
