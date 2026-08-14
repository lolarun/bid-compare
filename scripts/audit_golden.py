"""audit_golden.py — 独立审计标准答案 Excel（阶段一，不跑模型、不改识别代码）。

对每份报价/招标标准答案 Excel 做来源无关的结构 + 算术审计，输出：
  outputs/golden_audit/<doc>.json   机器可读审计结果
  outputs/golden_audit/<doc>.md     人工可读审计报告

审计内容（CLAUDE.md §5/§6 + 用户阶段一要求）：
- 表头列映射（处理同义列名：项目名称/品名、价税合计/合价(含税) 等）
- 序号完整性：范围、缺号、重复号
- 逐行算术关系：
    qty × 单价(不含税) ≈ 合计(不含税)
    合计(不含税) × 税率 ≈ 税额
    合计(不含税) + 税额 ≈ 价税合计(含税合价)
    单价(含税) × qty ≈ 合价(含税)   [若有含税单价列]
- 明细含税合计 vs 声明总价差异
- 字段来源推断标签：raw / derived / ambiguous（page/bbox 级 raw 确认留待阶段二 PDF 对照）
- 文件版本号 + SHA256（后续优化不得静默改 golden）

注意：本脚本只读 Excel，不读 PDF、不跑 OCR/LLM、不写数据库。
page/table/row/bbox 级别的 raw 来源确认在阶段二（渲染+OCR PDF）建立。

用法：
    python scripts/audit_golden.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DOCS = REPO / "tests" / "fixtures" / "documents" / "bid_list"
OUT_DIR = REPO / "outputs" / "golden_audit"

# 算术一致性容差：绝对 ≤0.05 元 或 相对 ≤0.5% 视为一致
_ABS_TOL = 0.05
_REL_TOL = 0.005

# ── 待审计文档配置 ──────────────────────────────────────────────────────────
DOCS_CFG = [
    {
        "name": "quote_taikelong",
        "xlsx": DOCS / "泰科龙投标清单.xlsx",
        "doc_type": "quote",
        "declared_total_ref": 1_067_616.41,
        # PDF 转置表同时展示含税和不含税行，Excel 各列均直接对应 PDF
        "pdf_field_sources": None,  # use default inference
    },
    {
        "name": "quote_miancun",
        "xlsx": DOCS / "上海绵存投标清单.xlsx",
        "doc_type": "quote",
        "declared_total_ref": 1_667_051.0,
        # PDF 审计结论（page 4 验证）：PDF 仅有"单价"和"合价"列，
        # 实际为含税价（seq1: PDF 单价=93, Excel unit_price_excl=82.3, 82.3×1.13=93）。
        # Excel 的不含税单价/合计/税额均为反算值，不能用于评价 OCR 原文准确率。
        "pdf_field_sources": {
            "unit_price_excl_tax": "derived",
            "total_price_excl_tax": "derived",
            "tax_amount": "derived",
            "unit_price_incl_tax": "derived",  # Excel 无此列；PDF "单价" 即含税但 Excel 未存
            "total_price_incl_tax": "raw",      # Excel "价税合计" = PDF "合价"
            "tax_rate": "ambiguous",            # PDF 投标书写 13%，非逐行标注
        },
    },
    {
        "name": "quote_kaishuo",
        "xlsx": DOCS / "凯硕新正投标清单.xlsx",
        "doc_type": "quote",
        "declared_total_ref": 932_154.0,
        # PDF 审计结论（page 4 验证）：PDF 同时展示不含税单价、不含税合计、
        # 税额、单价(含税)、价税合计。但 PDF 与 Excel 有分角级舍入差异
        #（seq2: PDF total_excl=1594.69 vs Excel=1594.77）。
        # 将 total_price_excl_tax / tax_amount 标 ambiguous：值可信但非精确匹配。
        "pdf_field_sources": {
            "total_price_excl_tax": "ambiguous",
            "tax_amount": "ambiguous",
        },
    },
]

# ── 表头同义词 → 标准字段 ────────────────────────────────────────────────────
HEADER_MAP = {
    "序号": "seq",
    "专业": "profession",
    "项目名称": "name", "品名": "name",
    "规格": "spec",
    "型号": "model",
    "工作压力": "pressure",
    "阀体": "m_体", "阀芯": "m_芯", "阀板": "m_板", "阀杆": "m_杆", "密封圈": "m_封",
    "单位": "unit",
    "数量": "qty",
    "单价(不含税)": "unit_price_excl_tax", "单价（不含税）": "unit_price_excl_tax",
    "合计(不含税)": "total_price_excl_tax", "合计（不含税）": "total_price_excl_tax",
    "税率": "tax_rate",
    "税额": "tax_amount",
    "单价(含税)": "unit_price_incl_tax", "单价（含税）": "unit_price_incl_tax",
    "合价(含税)": "total_price_incl_tax", "合价（含税）": "total_price_incl_tax",
    "价税合计": "total_price_incl_tax",
    "品牌": "brand",
    "备注": "remark", "备注/系统": "remark", "系统": "remark",
}

_NUM_FIELDS = {
    "qty", "unit_price_excl_tax", "total_price_excl_tax", "tax_rate",
    "tax_amount", "unit_price_incl_tax", "total_price_incl_tax",
}


def _coerce_num(v):
    """数字解析，支持百分号（'13%' → 0.13）。修复旧 _coerce_num 的 % bug。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    if s == "" or s == "-":
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    try:
        f = float(s)
        return f / 100.0 if pct else f
    except (ValueError, TypeError):
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _consistent(actual, expected) -> bool:
    """actual ≈ expected ？绝对或相对容差任一满足即一致。"""
    if actual is None or expected is None:
        return False
    d = abs(actual - expected)
    if d <= _ABS_TOL:
        return True
    base = max(abs(expected), 1.0)
    return d / base <= _REL_TOL


def _load_rows(xlsx: Path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))

    # 找表头行：含 '序号' 的第一行
    header_idx = None
    for i, r in enumerate(all_rows):
        cells = [str(c or "").strip() for c in r]
        if "序号" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"{xlsx.name}: 找不到表头行（无 '序号' 列）")

    headers = [str(c or "").strip() for c in all_rows[header_idx]]
    # 列索引映射：标准字段 → 列号（材质合并到 materials）
    col_field: dict[int, str] = {}
    for ci, h in enumerate(headers):
        f = HEADER_MAP.get(h)
        if f:
            col_field[ci] = f

    present_fields = set(col_field.values())

    data_rows = []
    total_rows = []   # 非数字序号（合计/小计/说明）
    for r in all_rows[header_idx + 1:]:
        rec: dict = {"materials": {}}
        for ci, f in col_field.items():
            val = r[ci] if ci < len(r) else None
            if f.startswith("m_"):
                sval = str(val or "").strip()
                if sval and sval != "-":
                    rec["materials"][f[2:]] = sval
            elif f in _NUM_FIELDS:
                rec[f] = _coerce_num(val)
            else:
                rec[f] = str(val or "").strip()
        seq = str(rec.get("seq") or "").strip()
        # 跳过完全空行
        if not seq and not rec.get("name"):
            continue
        if seq.isdigit():
            data_rows.append(rec)
        else:
            # 捕获合计/小计行里的含税合计值
            total_rows.append(rec)
    return ws.title, headers, present_fields, data_rows, total_rows


def _audit_row(rec: dict, present: set) -> list[dict]:
    """逐行算术一致性检查，返回不一致项列表。"""
    issues = []
    qty = rec.get("qty")
    upe = rec.get("unit_price_excl_tax")
    tpe = rec.get("total_price_excl_tax")
    tax = rec.get("tax_amount")
    rate = rec.get("tax_rate")
    upi = rec.get("unit_price_incl_tax")
    tpi = rec.get("total_price_incl_tax")

    # 1) qty × 不含税单价 ≈ 不含税合计
    if qty is not None and upe is not None and tpe is not None:
        if not _consistent(qty * upe, tpe):
            issues.append({"check": "qty×excl_unit=excl_total",
                           "lhs": round(qty * upe, 4), "rhs": tpe})
    # 2) 不含税合计 × 税率 ≈ 税额
    if tpe is not None and rate is not None and tax is not None:
        if not _consistent(tpe * rate, tax):
            issues.append({"check": "excl_total×rate=tax",
                           "lhs": round(tpe * rate, 4), "rhs": tax})
    # 3) 不含税合计 + 税额 ≈ 含税合计
    if tpe is not None and tax is not None and tpi is not None:
        if not _consistent(tpe + tax, tpi):
            issues.append({"check": "excl_total+tax=incl_total",
                           "lhs": round(tpe + tax, 4), "rhs": tpi})
    # 4) 含税单价 × qty ≈ 含税合计（若有含税单价列）
    if upi is not None and qty is not None and tpi is not None:
        if not _consistent(upi * qty, tpi):
            issues.append({"check": "incl_unit×qty=incl_total",
                           "lhs": round(upi * qty, 4), "rhs": tpi})
    return issues


def _infer_field_sources(present: set, doc_name: str,
                         pdf_overrides: dict | None = None) -> dict:
    """结构推断字段来源标签，支持 PDF 审计后的覆盖。

    默认按 Excel 列存在性推断；pdf_overrides 来自 DOCS_CFG 中
    经 PDF 视觉审计确认的 field→source 映射，优先级高于推断。
    """
    src = {}
    src["unit_price_incl_tax"] = "raw" if "unit_price_incl_tax" in present else "derived"
    src["total_price_incl_tax"] = "raw" if "total_price_incl_tax" in present else "derived"
    src["unit_price_excl_tax"] = "raw" if "unit_price_excl_tax" in present else "derived"
    src["total_price_excl_tax"] = "raw" if "total_price_excl_tax" in present else "derived"
    src["tax_amount"] = "raw" if "tax_amount" in present else "derived"
    src["tax_rate"] = "raw" if "tax_rate" in present else "ambiguous"
    src["qty"] = "raw"
    src["name"] = "raw"
    src["spec"] = "raw"
    if pdf_overrides:
        src.update(pdf_overrides)
    return src


def audit_doc(cfg: dict) -> dict:
    xlsx: Path = cfg["xlsx"]
    if not xlsx.exists():
        return {"name": cfg["name"], "error": f"{xlsx} 不存在"}

    sheet, headers, present, data_rows, total_rows = _load_rows(xlsx)

    # ── 序号审计 ──────────────────────────────────────────────────────────
    seqs = [int(r["seq"]) for r in data_rows]
    seq_set = set(seqs)
    seq_min, seq_max = (min(seqs), max(seqs)) if seqs else (0, 0)
    full = set(range(seq_min, seq_max + 1)) if seqs else set()
    seq_missing = sorted(full - seq_set)
    dup = sorted({s for s in seqs if seqs.count(s) > 1})

    # ── 逐行算术 ──────────────────────────────────────────────────────────
    row_issues = []
    for r in data_rows:
        iss = _audit_row(r, present)
        if iss:
            row_issues.append({"seq": r["seq"], "name": r.get("name"), "issues": iss})

    # ── 总价对账 ──────────────────────────────────────────────────────────
    incl_sum = round(sum((r.get("total_price_incl_tax") or 0) for r in data_rows), 2)
    excl_sum = round(sum((r.get("total_price_excl_tax") or 0) for r in data_rows), 2)
    tax_sum = round(sum((r.get("tax_amount") or 0) for r in data_rows), 2)

    # sheet 内声明总价（合计/小计行的含税值）
    sheet_declared = None
    for tr in total_rows:
        v = tr.get("total_price_incl_tax")
        if v is not None:
            sheet_declared = v
            break

    ref = cfg.get("declared_total_ref")
    declared = sheet_declared if sheet_declared is not None else ref

    return {
        "name": cfg["name"],
        "source_file": str(xlsx.relative_to(REPO)),
        "sha256": _sha256(xlsx),
        "version": "v1",
        "sheet": sheet,
        "headers": headers,
        "present_fields": sorted(present),
        "row_count": len(data_rows),
        "seq_audit": {
            "range": [seq_min, seq_max],
            "count": len(seqs),
            "missing": seq_missing,
            "duplicate": dup,
        },
        "arithmetic": {
            "rows_with_issues": len(row_issues),
            "total_rows": len(data_rows),
            "detail": row_issues,
        },
        "totals": {
            "incl_sum_from_lines": incl_sum,
            "excl_sum_from_lines": excl_sum,
            "tax_sum_from_lines": tax_sum,
            "excl+tax": round(excl_sum + tax_sum, 2),
            "sheet_declared_total": sheet_declared,
            "reference_total": ref,
            "declared_used": declared,
            "line_vs_declared_diff": (round(abs(incl_sum - declared), 2)
                                      if declared is not None else None),
        },
        "field_sources": _infer_field_sources(
            present, cfg["name"], cfg.get("pdf_field_sources")),
        "total_rows_raw": [
            {k: v for k, v in tr.items() if v not in (None, "", {})}
            for tr in total_rows
        ],
    }


def _write_md(audit: dict, path: Path):
    a = audit
    if "error" in a:
        path.write_text(f"# {a['name']}\n\n**ERROR**: {a['error']}\n", encoding="utf-8")
        return
    sa = a["seq_audit"]
    t = a["totals"]
    lines = [
        f"# Golden 审计 — {a['name']}",
        "",
        f"- 源文件: `{a['source_file']}`",
        f"- SHA256: `{a['sha256']}`  版本: {a['version']}",
        f"- Sheet: {a['sheet']}  行数: {a['row_count']}",
        f"- 字段: {', '.join(a['present_fields'])}",
        "",
        "## 序号完整性",
        f"- 范围: {sa['range'][0]}..{sa['range'][1]}  数量: {sa['count']}",
        f"- 缺号: {sa['missing'] or '无'}",
        f"- 重复号: {sa['duplicate'] or '无'}",
        "",
        "## 总价对账",
        f"- 明细含税合计: {t['incl_sum_from_lines']:,}",
        f"- 明细不含税合计 + 税额: {t['excl+tax']:,}",
        f"- Sheet 声明总价: {t['sheet_declared_total']}",
        f"- 参考总价: {t['reference_total']}",
        f"- 明细 vs 声明差异: {t['line_vs_declared_diff']}",
        "",
        "## 算术一致性",
        f"- 不一致行: {a['arithmetic']['rows_with_issues']} / {a['arithmetic']['total_rows']}",
    ]
    for ri in a["arithmetic"]["detail"][:30]:
        for iss in ri["issues"]:
            lines.append(f"  - seq={ri['seq']} {iss['check']}: {iss['lhs']} ≠ {iss['rhs']}")
    lines += [
        "",
        "## 字段来源推断（page/bbox 级 raw 确认留待阶段二）",
    ]
    for f, s in a["field_sources"].items():
        lines.append(f"- {f}: **{s}**")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== golden audit (阶段一，不跑模型) ===\n")
    summary = []
    for cfg in DOCS_CFG:
        audit = audit_doc(cfg)
        jpath = OUT_DIR / f"{audit['name']}.json"
        mpath = OUT_DIR / f"{audit['name']}.md"
        jpath.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_md(audit, mpath)
        if "error" in audit:
            print(f"[ERR] {audit['name']}: {audit['error']}")
            continue
        sa, t = audit["seq_audit"], audit["totals"]
        print(f"[ok] {audit['name']}:")
        print(f"     rows={audit['row_count']} seq={sa['range']} missing={sa['missing']} dup={sa['duplicate']}")
        print(f"     arith_issues={audit['arithmetic']['rows_with_issues']}/{audit['arithmetic']['total_rows']}")
        print(f"     incl_sum={t['incl_sum_from_lines']:,} declared={t['declared_used']} diff={t['line_vs_declared_diff']}")
        print(f"     incl_unit_price source={audit['field_sources']['unit_price_incl_tax']}")
        print()
        summary.append(audit)
    print(f"→ 输出: {OUT_DIR.relative_to(REPO)}/<doc>.{{json,md}}")


if __name__ == "__main__":
    main()
