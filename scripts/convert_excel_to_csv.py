"""将 docs/现有资料/材料汇总/ 下所有 Excel 文件的每个 sheet 导出为 CSV。"""

import sys
import json
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parent.parent / "docs" / "现有资料" / "材料汇总"
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"

CATEGORY_MAP = {
    "不锈钢管清单": ("给排水", "不锈钢管"),
    "风盘报价单格式": ("暖通", "风机盘管"),
    "风口风阀报价单格式": ("暖通", "风口风阀"),
    "空调泵询价格式": ("暖通", "空调泵"),
    "母线报价单格式模板": ("电气", "母线槽"),
    "配电箱": ("电气", "配电箱"),
    "潜水泵询价格式": ("给排水", "潜水泵"),
    "桥架报价单格式模板": ("电气", "桥架"),
    "阀门询价格式": ("给排水", "阀门"),
    "水箱报价清单": ("给排水", "水箱"),
}


def sanitize(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_").strip("_")


def convert_all():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    for f in sorted(SRC_DIR.iterdir()):
        if f.suffix not in (".xls", ".xlsx"):
            continue

        base_name = f.stem.lstrip("0").strip()
        engine = "xlrd" if f.suffix == ".xls" else "openpyxl"

        try:
            xls = pd.ExcelFile(f, engine=engine)
        except Exception as e:
            print(f"[ERROR] {f.name}: {e}", file=sys.stderr)
            continue

        profession, category = CATEGORY_MAP.get(base_name, ("未分类", base_name))

        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet, header=None)
            csv_name = f"{sanitize(base_name)}_{sanitize(sheet)}.csv"
            csv_path = OUT_DIR / csv_name
            df.to_csv(csv_path, index=False, header=False, encoding="utf-8-sig")

            manifest.append({
                "csv": csv_name,
                "source_file": f.name,
                "sheet": sheet,
                "profession": profession,
                "category": category,
                "rows": len(df),
                "cols": len(df.columns),
            })
            print(f"  {csv_name}  ({len(df)} rows)")

    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共导出 {len(manifest)} 个 CSV，manifest 写入 {manifest_path}")
    return manifest


if __name__ == "__main__":
    convert_all()
