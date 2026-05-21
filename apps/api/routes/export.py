"""Export endpoints — Excel download for all major data views."""
import io
from datetime import datetime
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models.material import Material
from apps.api.models.supplier import Supplier
from apps.api.models.quote import Quote
from apps.api.models.project import Project
from apps.api.services.bid_matrix import build_bid_matrix

router = APIRouter(prefix="/api/export", tags=["export"])

# ── Shared styles ────────────────────────────────────────────────────────────

_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
_GREEN_FILL = PatternFill(start_color="F6FFED", fill_type="solid")
_RED_FILL = PatternFill(start_color="FFF2F0", fill_type="solid")
_YELLOW_FILL = PatternFill(start_color="FFFBE6", fill_type="solid")


def _style_header(ws, col_count: int):
    """Apply header styling to row 1."""
    for col in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
        cell.border = _THIN_BORDER


def _auto_width(ws, min_width=10, max_width=40):
    """Auto-fit column widths based on content."""
    for col_cells in ws.columns:
        length = min_width
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value:
                length = max(length, min(len(str(cell.value)) + 2, max_width))
        ws.column_dimensions[col_letter].width = length


def _to_streaming(wb: Workbook, filename: str) -> StreamingResponse:
    """Serialize workbook to a streaming download response."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    # RFC 5987: use filename* with UTF-8 encoding for non-ASCII filenames
    encoded = url_quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        },
    )


# ── 1. Dashboard / 仪表盘报表 ────────────────────────────────────────────────

@router.get("/dashboard")
def export_dashboard(db: Session = Depends(get_db)):
    """导出仪表盘报表 — 包含采购概览 + 品类统计。"""
    from apps.api.services.statistics import get_dashboard_summary
    summary = get_dashboard_summary(db)

    wb = Workbook()
    ws = wb.active
    ws.title = "采购概览"

    # Summary cards
    ws.append(["指标", "数值"])
    ws.append(["累计入库材料", summary["total_materials"]])
    ws.append(["供应商数", summary["total_suppliers"]])
    ws.append(["项目数", summary["total_projects"]])
    ws.append(["报价条数", summary["total_quotes"]])
    _style_header(ws, 2)

    # Category breakdown sheet
    ws2 = wb.create_sheet("品类统计")
    cats = (
        db.query(Material.category, Quote.supplier_id)
        .outerjoin(Quote, Quote.material_id == Material.id)
        .all()
    )
    cat_stats: dict[str, dict] = {}
    for cat, sid in cats:
        if cat not in cat_stats:
            cat_stats[cat] = {"count": 0, "suppliers": set()}
        cat_stats[cat]["count"] += 1
        if sid:
            cat_stats[cat]["suppliers"].add(sid)

    ws2.append(["品类", "报价条数", "供应商数"])
    _style_header(ws2, 3)
    for cat, s in sorted(cat_stats.items(), key=lambda x: -x[1]["count"]):
        ws2.append([cat, s["count"], len(s["suppliers"])])

    _auto_width(ws)
    _auto_width(ws2)

    ts = datetime.now().strftime("%Y%m%d")
    return _to_streaming(wb, f"MEMPAS_仪表盘报表_{ts}.xlsx")


# ── 2. Suppliers / 供应商名单 ────────────────────────────────────────────────

@router.get("/suppliers")
def export_suppliers(db: Session = Depends(get_db)):
    """导出供应商名单。"""
    rows = db.query(Supplier).order_by(Supplier.id).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "供应商名单"

    ws.append(["ID", "供应商名称", "简称", "联系人", "电话", "经营品类", "中标次数", "合作评分", "备注"])
    _style_header(ws, 9)

    for s in rows:
        cats = ", ".join(s.categories) if isinstance(s.categories, list) else str(s.categories or "")
        ws.append([s.id, s.name, s.short_name, s.contact, s.phone, cats,
                    s.win_count, s.cooperation_score, s.remark])

    _auto_width(ws)
    ts = datetime.now().strftime("%Y%m%d")
    return _to_streaming(wb, f"MEMPAS_供应商名单_{ts}.xlsx")


# ── 3. Materials / 物料主数据 ────────────────────────────────────────────────

@router.get("/materials")
def export_materials(
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """导出物料主数据标准库。"""
    q = db.query(Material).order_by(Material.profession, Material.category, Material.id)
    if category:
        q = q.filter(Material.category == category)
    rows = q.all()

    wb = Workbook()
    ws = wb.active
    ws.title = "物料主数据"

    ws.append(["ID", "物料编码", "标准名称", "专业", "品类", "子类", "规格", "材质", "单位", "品牌", "执行标准", "参考均价"])
    _style_header(ws, 12)

    for m in rows:
        ws.append([m.id, m.material_code, m.standard_name, m.profession,
                    m.category, m.sub_category, m.spec, m.material_type,
                    m.unit, m.brand, m.exec_standard, m.ref_price_avg])

    _auto_width(ws)
    ts = datetime.now().strftime("%Y%m%d")
    cat_suffix = f"_{category}" if category else ""
    return _to_streaming(wb, f"MEMPAS_物料主数据{cat_suffix}_{ts}.xlsx")


# ── 4. Quotes / 采购数据(历史记录) ───────────────────────────────────────────

@router.get("/quotes")
def export_quotes(
    category: str | None = Query(None),
    supplier_id: int | None = Query(None),
    project_id: int | None = Query(None),
    alert_level: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """导出采购历史数据（支持筛选条件透传）。"""
    q = (
        db.query(Quote, Material.standard_name, Material.spec, Material.category,
                 Supplier.name.label("supplier_name"), Project.name.label("project_name"))
        .outerjoin(Material, Quote.material_id == Material.id)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .outerjoin(Project, Quote.project_id == Project.id)
    )
    if category:
        q = q.filter(Material.category == category)
    if supplier_id:
        q = q.filter(Quote.supplier_id == supplier_id)
    if project_id:
        q = q.filter(Quote.project_id == project_id)
    if alert_level:
        q = q.filter(Quote.alert_level == alert_level)

    rows = q.order_by(Quote.id.desc()).limit(10000).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "采购数据"

    ws.append(["ID", "物料名称", "规格", "品类", "供应商", "项目",
               "单价", "单价(不含税)", "税率", "数量", "总价",
               "偏差率", "告警", "报价日期"])
    _style_header(ws, 14)

    alert_fills = {"red": _RED_FILL, "yellow": _YELLOW_FILL, "normal": _GREEN_FILL}

    for quote, mat_name, spec, cat, sup_name, proj_name in rows:
        row_idx = ws.max_row + 1
        ws.append([
            quote.id, mat_name, spec, cat, sup_name, proj_name,
            quote.unit_price, quote.unit_price_excl_tax, quote.tax_rate,
            quote.quantity, quote.total_price,
            f"{quote.deviation_pct * 100:.1f}%" if quote.deviation_pct is not None else "",
            quote.alert_level or "normal",
            quote.quote_date.strftime("%Y-%m-%d") if quote.quote_date else "",
        ])
        # Color alert column
        fill = alert_fills.get(quote.alert_level or "normal")
        if fill:
            ws.cell(row=row_idx, column=13).fill = fill

    _auto_width(ws)
    ts = datetime.now().strftime("%Y%m%d")
    return _to_streaming(wb, f"MEMPAS_采购数据_{ts}.xlsx")


# ── 5. Bid Matrix / 比价矩阵 ────────────────────────────────────────────────

@router.get("/bid-matrix")
def export_bid_matrix(
    supplier_ids: str = Query(..., description="逗号分隔的供应商ID"),
    project_id: int | None = Query(None),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """导出横向比价矩阵为 Excel（带色标）。"""
    sids = [int(x) for x in supplier_ids.split(",") if x.strip()]
    result = build_bid_matrix(db, supplier_ids=sids, project_id=project_id, category=category)

    wb = Workbook()
    ws = wb.active
    ws.title = "比价矩阵"

    suppliers = result["suppliers"]
    # Header row
    header = ["材料", "规格", "历史均价", "合理史低"]
    for s in suppliers:
        header += [f"{s['letter']} {s['name']}(单价)", f"{s['letter']}(偏差)", f"{s['letter']}(告警)"]
    header += ["最低偏差", "推荐"]
    ws.append(header)
    _style_header(ws, len(header))

    alert_fills = {"red": _RED_FILL, "yellow": _YELLOW_FILL, "normal": _GREEN_FILL}

    for row in result["rows"]:
        data = [
            row["material_name"],
            row.get("spec", ""),
            row["historical_avg"]["price"] if row.get("historical_avg") else "",
            row["reasonable_low"]["price"] if row.get("reasonable_low") else "",
        ]
        for cell in row["suppliers"]:
            data.append(cell["price"] if cell["price"] is not None else "")
            data.append(f"{cell['deviation_pct'] * 100:.1f}%" if cell.get("deviation_pct") is not None else "")
            data.append(cell.get("alert_level", ""))
        data.append(f"{row['min_deviation'] * 100:.1f}%" if row.get("min_deviation") is not None else "")
        data.append(row.get("recommended", ""))

        row_idx = ws.max_row + 1
        ws.append(data)

        # Color alert cells
        for si, cell in enumerate(row["suppliers"]):
            alert_col = 4 + si * 3 + 3  # alert column for this supplier
            fill = alert_fills.get(cell.get("alert_level", "normal"))
            if fill:
                ws.cell(row=row_idx, column=alert_col).fill = fill

    # Totals row
    totals_data = ["汇总", "", "", ""]
    totals_map = {t["supplier_id"]: t for t in result["totals"]}
    for s in suppliers:
        t = totals_map.get(s["id"])
        totals_data.append(f"¥{t['total']:,.0f}" if t else "")
        totals_data.append(f"{t['avg_deviation'] * 100:.1f}%" if t else "")
        totals_data.append("")
    totals_data += ["", ""]
    ws.append(totals_data)
    # Bold totals
    for col in range(1, len(header) + 1):
        ws.cell(row=ws.max_row, column=col).font = Font(bold=True)

    _auto_width(ws)
    ts = datetime.now().strftime("%Y%m%d")
    cat_suffix = f"_{category}" if category else ""
    return _to_streaming(wb, f"MEMPAS_比价矩阵{cat_suffix}_{ts}.xlsx")


# ── 6. Logs / 操作日志 ──────────────────────────────────────────────────────

@router.get("/logs")
def export_logs():
    """导出操作日志（当前为占位 — 日志模块待实装后对接）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "操作日志"

    ws.append(["时间", "操作人", "模块", "操作类型", "对象", "结果", "详情"])
    _style_header(ws, 7)

    # Placeholder: 当日志模块实装后，此处从 AuditLog 表查询
    ws.append(["暂无日志数据 — 审计日志模块尚未实装", "", "", "", "", "", ""])

    _auto_width(ws)
    ts = datetime.now().strftime("%Y%m%d")
    return _to_streaming(wb, f"MEMPAS_操作日志_{ts}.xlsx")
