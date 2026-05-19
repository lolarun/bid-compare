"""Generate test documents from docs/data/ CSVs for e2e accuracy testing.

Renders a small quote-style table as PNG using high-DPI text drawing
to simulate a scanned/photographed supplier bid sheet. Saves alongside
a JSON ground-truth file.

Run: uv run python apps/api/tests/fixtures/generate_test_docs.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"

CN_FONT_PATHS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def _font(size: int) -> ImageFont.ImageFont:
    for p in CN_FONT_PATHS:
        if Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ─── Quote document ────────────────────────────────────────────────────────
def make_real_quote_image() -> tuple[bytes, dict]:
    """Render a real-data quote table from docs/data/阀门询价格式_汇总.csv.

    Picks the first 4 valid data rows and produces a clear PNG mimicking
    a scanned supplier bid sheet. Returns (png_bytes, ground_truth_dict).
    """
    csv_path = DATA_DIR / "阀门询价格式_汇总.csv"
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    # Drop the format-template header row (序号 is NaN there)
    df = df[df["序号"].notna()].reset_index(drop=True)
    # Take 4 rows that look like clean valve entries
    df = df.head(4).copy()

    # The CSV uses 项目名称 for the material name and an ex-tax price column.
    # Locate columns robustly by partial match (since headers contain newlines).
    name_col = next(c for c in df.columns if "项目名称" in str(c))
    spec_col = next(c for c in df.columns if str(c).strip() == "规格")  # DN size
    model_col = next(c for c in df.columns if "型号" in str(c))
    brand_col = next(c for c in df.columns if "品牌" in str(c))
    unit_col = next(c for c in df.columns if "单位" in str(c))
    qty_col = next(c for c in df.columns if str(c).strip() == "数量")
    # `单价（元）\n不含税` — ex-tax unit price
    ex_tax_col = next(c for c in df.columns if "单价" in str(c) and "不含税" in str(c))
    # `价税合计（元）` — total with tax
    incl_total_col = next(c for c in df.columns if "价税合计" in str(c))

    # Build ground truth list (canonical fields)
    items = []
    for _, row in df.iterrows():
        ex_tax = row.get(ex_tax_col)
        incl_total = row.get(incl_total_col)
        qty = row.get(qty_col)
        incl_price = (incl_total / qty) if pd.notna(incl_total) and pd.notna(qty) and qty else None
        items.append({
            "material": str(row.get(name_col) or "").strip(),
            "spec": str(row.get(spec_col) or "").strip(),
            "model": str(row.get(model_col) or "").strip(),
            "brand": str(row.get(brand_col) or "").strip(),
            "unit": str(row.get(unit_col) or "").strip(),
            "qty": float(qty) if pd.notna(qty) else None,
            "unit_price_excl_tax": float(ex_tax) if pd.notna(ex_tax) else None,
            "unit_price": round(float(incl_price), 2) if incl_price is not None else None,
        })

    ground_truth = {
        "supplier_name": "测试阀门供应商",
        "quote_date": "2025-12-10",
        "items": items,
    }

    # Render as image
    title_font = _font(22)
    head_font = _font(17)
    cell_font = _font(15)

    cols = ["序号", "项目名称", "规格", "型号", "品牌", "单位", "数量", "含税单价"]
    col_widths = [50, 420, 80, 150, 80, 50, 50, 100]
    row_h = 60
    pad = 14
    width = sum(col_widths) + 2 * pad
    height = 130 + row_h * (len(items) + 1)

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    # Header
    d.text((pad, 16), f"投标单位：{ground_truth['supplier_name']}", font=title_font, fill="black")
    d.text((pad, 50), f"投标日期：{ground_truth['quote_date']}     联系电话：021-12345678",
           font=head_font, fill="black")

    # Table header row
    y = 100
    x = pad
    for c, w in zip(cols, col_widths):
        d.rectangle([x, y, x + w, y + row_h], outline="black")
        d.text((x + 4, y + 12), c, font=head_font, fill="black")
        x += w

    # Data rows — render the ground-truth items we built above
    for i, item in enumerate(items):
        y += row_h
        x = pad
        # Material can be long; word-wrap by inserting newlines if needed (cell_font handles)
        material_display = item["material"]
        price_display = f"{item['unit_price']:.2f}" if item["unit_price"] is not None else ""
        values = [
            str(i + 1),
            material_display,
            item["spec"],
            item["model"],
            item["brand"],
            item["unit"],
            f"{item['qty']:g}" if item["qty"] is not None else "",
            price_display,
        ]
        for v, w in zip(values, col_widths):
            d.rectangle([x, y, x + w, y + row_h], outline="black")
            d.text((x + 4, y + 14), v, font=cell_font, fill="black")
            x += w

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), ground_truth


def make_real_tender_image() -> tuple[bytes, dict]:
    """Render a tender-style material list from docs/data/桥架报价单格式模板_汇总.csv."""
    csv_path = DATA_DIR / "桥架报价单格式模板_汇总.csv"
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["序号"].notna()].reset_index(drop=True)
    df = df.head(5).copy()

    items = []
    for _, row in df.iterrows():
        items.append({
            "name": str(row.get("名称") or "").strip(),
            "category": "桥架",
            "spec": str(row.get("规格") or "").strip(),
            "unit": str(row.get("单位") or "").strip(),
            "quantity": float(row.get("数量") or 0) if pd.notna(row.get("数量")) else None,
        })

    ground_truth = {
        "project_name": "测试机电材料采购招标 — 桥架部分",
        "project_code": "TEST-BID-2026-001",
        "tender_date": "2026-05-20",
        "deadline": "2026-06-15",
        "items": items,
    }

    title_font = _font(24)
    head_font = _font(18)
    cell_font = _font(16)

    cols = ["序号", "名称", "规格", "材质", "单位", "数量"]
    col_widths = [60, 360, 180, 140, 60, 60]
    row_h = 48
    pad = 14
    width = sum(col_widths) + 2 * pad
    height = 150 + row_h * (len(items) + 1)

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    d.text((pad, 14), f"项目名称：{ground_truth['project_name']}", font=title_font, fill="black")
    d.text((pad, 50), f"招标编号：{ground_truth['project_code']}     "
                       f"招标日期：{ground_truth['tender_date']}",
           font=head_font, fill="black")
    d.text((pad, 80), f"投标截止：{ground_truth['deadline']}     "
                       f"专业：电气（桥架）", font=head_font, fill="black")

    y = 118
    x = pad
    for c, w in zip(cols, col_widths):
        d.rectangle([x, y, x + w, y + row_h], outline="black")
        d.text((x + 4, y + 10), c, font=head_font, fill="black")
        x += w

    for _, row in df.iterrows():
        y += row_h
        x = pad
        values = [
            str(int(row["序号"])),
            str(row.get("名称") or "")[:24],
            str(row.get("规格") or ""),
            str(row.get("材质") or ""),
            str(row.get("单位") or ""),
            str(int(row.get("数量"))) if pd.notna(row.get("数量")) else "",
        ]
        for v, w in zip(values, col_widths):
            d.rectangle([x, y, x + w, y + row_h], outline="black")
            d.text((x + 4, y + 12), v, font=cell_font, fill="black")
            x += w

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), ground_truth


if __name__ == "__main__":
    quote_png, quote_truth = make_real_quote_image()
    (HERE / "real_quote.png").write_bytes(quote_png)
    (HERE / "real_quote_truth.json").write_text(
        json.dumps(quote_truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote real_quote.png ({len(quote_png)} bytes) and ground truth ({len(quote_truth['items'])} items)")

    tender_png, tender_truth = make_real_tender_image()
    (HERE / "real_tender.png").write_bytes(tender_png)
    (HERE / "real_tender_truth.json").write_text(
        json.dumps(tender_truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote real_tender.png ({len(tender_png)} bytes) and ground truth ({len(tender_truth['items'])} items)")
