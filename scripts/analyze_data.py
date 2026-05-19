"""分析 docs/data/ 下的原始CSV数据，产出分析报告和统计表。"""

import json
import re
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "docs" / "data"
OUT_DIR = DATA_DIR / "analysis"
REPORT_PATH = BASE / "docs" / "design" / "05-数据分析报告.md"

# ---------------------------------------------------------------------------
# 1. Category-specific parsers — each returns a standardised DataFrame
# ---------------------------------------------------------------------------

def _to_float(s):
    if pd.isna(s):
        return np.nan
    s = str(s).strip()
    s = s.replace(",", ".").replace("，", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_桥架(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=0, dtype=str, encoding="utf-8-sig")
    df.columns = [
        "序号", "名称", "规格", "材质", "侧板厚度", "底板厚度", "盖板厚度",
        "数量", "单位", "本体单价含税", "盖板单价含税", "本体及盖板合价含税",
        "合计含税", "品牌", "执行标准", "备注",
    ]
    df = df[df["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    df["单价"] = df["本体单价含税"].apply(_to_float)
    df["数量_n"] = df["数量"].apply(_to_float)
    return df


def parse_阀门(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    # rows 0-1 are headers, row 2 is sub-header, data from row 3
    data = df.iloc[3:].copy()
    data.columns = [
        "序号", "专业", "名称", "规格", "型号", "工作压力",
        "阀体材质", "阀芯材质", "阀板材质", "阀杆材质", "密封圈材质",
        "单位", "数量", "单价不含税", "合计不含税",
        "税率", "税额", "价税合计", "品牌", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["价税合计"].apply(_to_float) / data["数量"].apply(_to_float)
    data["单价_不含税"] = data["单价不含税"].apply(_to_float)
    return data


def parse_风口风阀(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "型号", "规格", "钢板厚度",
        "数量", "单位", "含税单价", "价税合计", "品牌", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["含税单价"].apply(_to_float)
    data["数量_n"] = data["数量"].apply(_to_float)
    return data


def parse_母线(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "母线类型", "规格型号", "铜牌厚度及开关型号",
        "接地方式", "防护等级", "外壳材质", "品牌", "系列",
        "单位", "数量", "不含税单价", "税率", "含税单价", "含税合价", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["含税单价"].apply(_to_float)
    data["规格"] = data["规格型号"]
    return data


def parse_不锈钢管(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "规格", "壁厚", "牌号", "连接方式", "承压",
        "单位", "工程量", "不含税单价", "不含税总价",
        "含税单价", "含税总价", "品牌", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["含税单价"].apply(_to_float)
    data["数量_n"] = data["工程量"].apply(_to_float)
    return data


def parse_水箱(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "规格型号", "单位", "数量",
        "单价不含税", "金额不含税", "税率", "税额",
        "价税合计", "位置及用途", "品牌", "材质", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["价税合计"].apply(_to_float) / data["数量"].apply(_to_float)
    data["规格"] = data["规格型号"]
    return data


def parse_潜水泵(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "型号", "流量Q", "扬程H", "转数", "功率N",
        "数量", "税率", "单价", "总价", "品牌", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["单价"].apply(_to_float)
    data["规格"] = data["型号"]
    return data


def parse_风机盘管(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    data = df.iloc[1:].copy()
    data.columns = [
        "序号", "名称", "型号", "管制", "风量",
        "数量", "单位", "过滤网材质",
        "本体价格", "回风箱价格", "滴水盘价格",
        "单价合计", "价税合计", "品牌", "备注",
    ]
    data = data[data["序号"].apply(lambda x: str(x).strip().isdigit())].copy()
    data["单价"] = data["单价合计"].apply(_to_float)
    data["规格"] = data["型号"]
    return data


def parse_空调泵(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    ncols = len(df.columns)
    data = df.iloc[2:].copy()
    data["名称"] = data.iloc[:, 0]
    data["规格"] = data.iloc[:, 1]
    data["流量"] = data.iloc[:, 2]
    data["扬程"] = data.iloc[:, 3]
    data["功率"] = data.iloc[:, 5]
    # last 6 cols: 数量, 税率, 单价, 总价, 品牌, 备注
    data["数量"] = data.iloc[:, ncols - 6]
    data["税率"] = data.iloc[:, ncols - 5]
    data["单价"] = data.iloc[:, ncols - 4].apply(_to_float)
    data["总价"] = data.iloc[:, ncols - 3]
    data["品牌"] = data.iloc[:, ncols - 2]
    data["备注"] = data.iloc[:, ncols - 1]
    data = data[data["名称"].notna() & (data["名称"].str.strip() != "")].copy()
    data = data[~data["名称"].str.contains("设备名称|名称", na=False)].copy()
    # filter out rows where 品牌 looks like a number (parsing error)
    def _is_brand(v):
        if pd.isna(v):
            return False
        s = str(v).strip()
        try:
            float(s)
            return False
        except ValueError:
            return bool(s)
    data.loc[~data["品牌"].apply(_is_brand), "品牌"] = np.nan
    return data


def parse_配电箱_all(manifest) -> pd.DataFrame:
    """配电箱是BOM结构，提取每个箱子的总价。"""
    rows = []
    for entry in manifest:
        if entry["category"] != "配电箱":
            continue
        path = DATA_DIR / entry["csv"]
        df = pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
        project = entry["sheet"]
        current_box = None
        for _, row in df.iterrows():
            vals = [str(v).strip() if pd.notna(v) else "" for v in row]
            # detect box header: first col like "1-1" or "配电箱柜"
            if len(vals) > 1 and re.match(r"\d+-\d+", vals[0]):
                current_box = vals[1] + " " + vals[2] if len(vals) > 2 else vals[1]
            # detect total row
            if any("单台合计" in v or "总计" in v for v in vals[:3]):
                price = None
                for v in vals[3:]:
                    p = _to_float(v)
                    if p and p > 0:
                        price = p
                        break
                if price and current_box:
                    rows.append({
                        "项目": project,
                        "名称": current_box,
                        "单价": price,
                        "品牌": "",
                    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Sub-category extraction (heuristic keyword matching)
# ---------------------------------------------------------------------------

SUBCAT_RULES = {
    "桥架": [
        ("梯架|梯式", "梯式桥架"),
        ("槽式", "槽式桥架"),
        ("线槽", "线槽"),
        ("防水", "防水桥架"),
        ("竖井", "竖井桥架"),
        ("铝合金", "铝合金桥架"),
        ("复合|彩钢|玻璃钢", "复合桥架"),
        ("弱电", "弱电桥架"),
        ("托盘", "托盘式桥架"),
        ("桥架", "普通桥架"),
    ],
    "阀门": [
        ("倒流防止", "倒流防止器"),
        ("水位控制", "水位控制阀"),
        ("排气阀", "排气阀"),
        ("Y.*过滤|过滤器", "过滤器"),
        ("止回", "止回阀"),
        ("球阀", "球阀"),
        ("蝶阀", "蝶阀"),
        ("闸阀", "闸阀"),
        ("减压", "减压阀"),
        ("平衡", "平衡阀"),
        ("电动", "电动阀"),
        ("阀", "其他阀门"),
    ],
    "风口风阀": [
        ("消声", "消声器/静压箱"),
        ("静压", "消声器/静压箱"),
        ("防火阀|FD|MEC|MEE|FVDK", "防火阀"),
        ("排烟阀|BEC|BEE", "排烟阀"),
        ("调节阀|调节风阀|调节蝶阀|VD|VCD|MD|MVD", "调节阀"),
        ("电动.*阀|开关阀|电动风阀|FM", "电动风阀"),
        ("止回阀|NRD", "止回阀"),
        ("定风量|CAV", "定风量阀"),
        ("送风口|排烟口|正压|GF|GP|GS|PS", "送风口/排烟口"),
        ("格栅|AH|AV|BH", "格栅风口"),
        ("百叶|自垂|门铰|HH", "百叶风口"),
        ("散流器|旋流|SD|FP|条形", "散流器"),
        ("风口", "其他风口"),
    ],
    "母线槽": [
        ("密集", "密集型母线槽"),
        ("空气", "空气型母线槽"),
        ("耐火", "耐火型母线槽"),
        ("插接箱|插接", "插接箱"),
        ("母线", "母线槽"),
    ],
    "不锈钢管": [
        ("316L|316", "316/316L不锈钢管"),
        ("304", "304不锈钢管"),
        ("雨水", "不锈钢雨水管"),
        ("管件|弯头|三通|接头", "管件"),
        ("卡箍|连接", "连接件"),
        ("", "不锈钢管"),
    ],
    "水箱": [
        ("消毒", "消毒器"),
        ("集水箱|污水", "污水集水箱"),
        ("水箱", "不锈钢水箱"),
    ],
    "潜水泵": [
        ("控制柜|控制箱", "控制柜"),
        ("自耦|导杆|链条|浮球|电缆", "附件"),
        ("泵|潜水", "潜水泵"),
    ],
    "风机盘管": [
        ("卧式", "卧式风机盘管"),
        ("立式", "立式风机盘管"),
        ("盘管", "风机盘管"),
    ],
    "空调泵": [
        ("卧式", "卧式离心泵"),
        ("立式", "立式离心泵"),
        ("泵", "离心泵"),
    ],
    "配电箱": [
        ("动力", "动力配电箱"),
        ("照明", "照明配电箱"),
        ("配电", "配电箱"),
    ],
}


def classify_subcat(name: str, category: str) -> str:
    if pd.isna(name):
        return "未分类"
    name = str(name).strip()
    rules = SUBCAT_RULES.get(category, [])
    for pattern, label in rules:
        if pattern and re.search(pattern, name, re.IGNORECASE):
            return label
    return "其他"


# ---------------------------------------------------------------------------
# 3. Analysis functions
# ---------------------------------------------------------------------------

def analyze_category(df: pd.DataFrame, category: str, manifest_entries: list) -> dict:
    """Run all analyses on one category's data."""
    result = {"category": category}

    # -- basic stats
    n_total = len(df)
    price_col = "单价"
    df["_price"] = df[price_col].apply(_to_float) if df[price_col].dtype == object else df[price_col]
    valid = df[df["_price"].notna() & (df["_price"] > 0)]
    n_valid = len(valid)
    result["n_total"] = n_total
    result["n_valid_price"] = n_valid
    result["price_missing_pct"] = round((n_total - n_valid) / max(n_total, 1) * 100, 1)

    # -- projects
    n_projects = len([e for e in manifest_entries if e["sheet"] != "汇总"
                      and not e["sheet"].startswith("Sheet")
                      and "计价" not in e["sheet"]
                      and "参数" not in e["sheet"]
                      and "明细" not in e["sheet"]
                      and "投标" not in e["sheet"]
                      and "技术" not in e["sheet"]])
    result["n_projects"] = n_projects

    # -- brands
    brand_col = "品牌" if "品牌" in df.columns else None
    brands = []
    if brand_col:
        raw = df[brand_col].dropna().unique()
        for b in raw:
            s = re.sub(r"\s+", "", str(b).strip())
            if not s or s == "nan":
                continue
            try:
                float(s)
                continue
            except ValueError:
                pass
            if s == "0" or s == "0.0":
                continue
            brands.append(s)
        brands = list(dict.fromkeys(brands))
    result["brands"] = brands
    result["n_brands"] = len(brands)

    # -- sub-category classification
    name_col = "名称" if "名称" in df.columns else None
    if name_col:
        df["子类"] = df[name_col].apply(lambda x: classify_subcat(x, category))
    else:
        df["子类"] = "未分类"

    subcat_counts = df["子类"].value_counts().to_dict()
    result["subcat_counts"] = subcat_counts

    # -- price distribution per sub-category
    price_stats = []
    for subcat, group in valid.groupby(df.loc[valid.index, "子类"]):
        prices = group["_price"]
        if len(prices) < 1:
            continue
        stats = {
            "子类": subcat,
            "count": len(prices),
            "mean": round(prices.mean(), 2),
            "median": round(prices.median(), 2),
            "std": round(prices.std(), 2) if len(prices) > 1 else 0,
            "cv": round(prices.std() / prices.mean(), 3) if len(prices) > 1 and prices.mean() > 0 else 0,
            "min": round(prices.min(), 2),
            "max": round(prices.max(), 2),
            "p10": round(np.percentile(prices, 10), 2) if len(prices) >= 5 else round(prices.min(), 2),
            "p90": round(np.percentile(prices, 90), 2) if len(prices) >= 5 else round(prices.max(), 2),
        }
        stats["suggested_threshold"] = round(max(stats["cv"] * 1.5, 0.05), 3)
        price_stats.append(stats)
    result["price_stats"] = sorted(price_stats, key=lambda x: -x["count"])

    # -- date extraction from remarks
    dates = []
    remark_col = "备注" if "备注" in df.columns else None
    if remark_col:
        for v in df[remark_col].dropna().unique():
            found = re.findall(r"20\d{2}[\./]\d{1,2}[\./]\d{1,2}", str(v))
            dates.extend(found)
    def _date_sort_key(d):
        parts = re.split(r"[\./]", d)
        return tuple(int(p) for p in parts)
    result["dates"] = sorted(set(dates), key=_date_sort_key)

    # -- sufficiency assessment
    result["sufficiency"] = assess_sufficiency(result)

    return result


def assess_sufficiency(r: dict) -> dict:
    """Evaluate data sufficiency for bidding comparison."""
    score = 0
    notes = []

    # project coverage
    if r["n_projects"] >= 5:
        score += 3
        notes.append(f"项目覆盖充足({r['n_projects']}个)")
    elif r["n_projects"] >= 3:
        score += 2
        notes.append(f"项目覆盖一般({r['n_projects']}个)")
    else:
        score += 1
        notes.append(f"项目覆盖不足({r['n_projects']}个，建议≥3)")

    # supplier diversity
    if r["n_brands"] >= 3:
        score += 3
        notes.append(f"供应商多样性好({r['n_brands']}家)")
    elif r["n_brands"] >= 2:
        score += 2
        notes.append(f"供应商偏少({r['n_brands']}家)")
    elif r["n_brands"] == 1:
        score += 1
        notes.append(f"仅单一供应商，无法横向比价")
    else:
        score += 0
        notes.append(f"无供应商/品牌数据，无法比价")

    # data volume
    if r["n_valid_price"] >= 100:
        score += 3
        notes.append(f"有效价格数据充足({r['n_valid_price']}条)")
    elif r["n_valid_price"] >= 30:
        score += 2
        notes.append(f"有效价格数据一般({r['n_valid_price']}条)")
    else:
        score += 1
        notes.append(f"有效价格数据偏少({r['n_valid_price']}条)")

    # price completeness
    if r["price_missing_pct"] < 5:
        score += 2
    elif r["price_missing_pct"] < 20:
        score += 1
        notes.append(f"价格缺失率{r['price_missing_pct']}%")
    else:
        notes.append(f"价格缺失率高({r['price_missing_pct']}%)")

    # overall verdict
    if r["n_brands"] == 0:
        verdict = "⚠️ 勉强支撑（无品牌数据）"
    elif score >= 9:
        verdict = "✅ 可支撑比价"
    elif score >= 6:
        verdict = "⚠️ 勉强支撑"
    else:
        verdict = "❌ 不足以支撑"

    # per-round assessment
    round1 = "✅" if r["n_valid_price"] >= 30 else "⚠️" if r["n_valid_price"] >= 10 else "❌"
    round2 = "✅" if r["n_brands"] >= 3 else "⚠️" if r["n_brands"] >= 2 else "❌"
    round3 = "✅" if r["n_brands"] >= 3 and r["n_projects"] >= 3 else "⚠️" if r["n_brands"] >= 2 else "❌"

    return {
        "verdict": verdict,
        "score": score,
        "notes": notes,
        "round1_price_ref": round1,
        "round2_multi_supplier": round2,
        "round3_full_evaluation": round3,
    }


# ---------------------------------------------------------------------------
# 4. Attribute recommendations
# ---------------------------------------------------------------------------

LAYER3_TECH_PARAMS = {
    "桥架": [
        ("规格(宽×高)", "规格", "匹配"),
        ("材质", "材质", "匹配"),
        ("是否含防火", "名称(关键词)", "匹配"),
        ("侧板厚度(mm)", "侧板厚度mm", "差异"),
        ("底板/横档厚度(mm)", "底板/横档厚度mm", "差异"),
        ("盖板厚度(mm)", "盖板厚度mm", "差异"),
        ("表面处理", "材质(热浸镀锌/热镀锌/彩钢)", "差异"),
    ],
    "阀门": [
        ("公称直径DN", "规格型号", "匹配"),
        ("工作压力(MPa)", "工作压力", "匹配"),
        ("阀体材质", "材质-阀体", "匹配"),
        ("连接方式", "连接方式(法兰/丝扣/对夹)", "匹配"),
        ("阀芯材质", "材质-阀芯", "差异"),
        ("阀板材质", "材质-阀板", "差异"),
        ("阀杆材质", "材质-阀杆", "差异"),
        ("密封圈材质", "密封圈", "差异"),
        ("型号", "型号", "差异"),
    ],
    "风口风阀": [
        ("型号系列", "型号", "匹配"),
        ("规格尺寸(mm)", "规格", "匹配"),
        ("防火等级", "名称(70°/150°/280°)", "匹配"),
        ("钢板厚度(mm)", "镀锌钢板/不锈钢板厚度", "差异"),
        ("是否带反馈信号", "名称(关键词)", "差异"),
        ("执行机构类型", "手动/半自动/全自动", "差异"),
        ("材质", "镀锌/不锈钢", "差异"),
    ],
    "母线槽": [
        ("母线类型", "母线类型(三相五线制等)", "匹配"),
        ("额定电流(A)", "规格型号", "匹配"),
        ("防护等级(IP)", "防护等级", "匹配"),
        ("铜牌厚度", "铜牌厚度及开关型号", "差异"),
        ("接地方式(PE/PEN)", "接地方式", "差异"),
        ("外壳材质", "外壳材质", "差异"),
        ("系列", "系列", "差异"),
    ],
    "配电箱": [
        ("回路数/配置", "规格型号", "匹配"),
        ("主要元器件规格", "元器件清单(BOM)", "匹配"),
        ("元器件品牌", "BOM中品牌列", "差异"),
        ("箱体材质/尺寸", "—", "差异"),
        ("进线方式", "—", "差异"),
        ("安装方式(明装/暗装)", "—", "差异"),
    ],
    "不锈钢管": [
        ("公称直径DN", "规格", "匹配"),
        ("材质牌号", "牌号(304/316/316L)", "匹配"),
        ("壁厚(mm)", "壁厚", "匹配"),
        ("连接方式", "连接方式(焊接/卡压/沟槽)", "匹配"),
        ("承压等级", "承压", "差异"),
    ],
    "水箱": [
        ("容积(m³)", "规格(长×宽×高)计算", "匹配"),
        ("材质等级", "SUS304/304L/316L/444", "匹配"),
        ("底板厚度", "板厚", "差异"),
        ("侧板厚度", "板厚", "差异"),
        ("顶板厚度", "板厚", "差异"),
        ("附件配置", "液位计/扶梯/检修孔等", "差异"),
    ],
    "潜水泵": [
        ("流量Q(m³/h)", "流量", "匹配"),
        ("扬程H(m)", "扬程", "匹配"),
        ("功率N(kW)", "功率", "匹配"),
        ("转速(r/min)", "转速", "差异"),
        ("泵体材质", "材质", "差异"),
        ("控制方式", "一控一/一控二/一用一备", "差异"),
    ],
    "风机盘管": [
        ("管制", "两管制/四管制", "匹配"),
        ("风量(m³/h)", "设计/投标中档风量", "匹配"),
        ("过滤网材质", "过滤网材质", "差异"),
        ("是否含回风箱", "配件配置", "差异"),
        ("是否含滴水盘", "配件配置", "差异"),
        ("排水方式", "—", "差异"),
    ],
    "空调泵": [
        ("流量(m³/h)", "流量", "匹配"),
        ("扬程(m)", "扬程", "匹配"),
        ("功率(kW)", "功率", "匹配"),
        ("转速(r/min)", "转速", "差异"),
        ("泵体材质", "泵体材质", "差异"),
        ("叶轮材质", "叶轮材质", "差异"),
        ("泵轴材质", "泵轴材质", "差异"),
        ("机封品牌", "机封", "差异"),
        ("电机能效", "电机能效", "差异"),
        ("电机防护等级", "电机防护等级", "差异"),
    ],
}


PROFESSION_MAP = {
    "桥架": "电气", "母线槽": "电气", "配电箱": "电气",
    "阀门": "给排水", "不锈钢管": "给排水", "水箱": "给排水", "潜水泵": "给排水",
    "风口风阀": "暖通", "风机盘管": "暖通", "空调泵": "暖通",
}

ALGO_ASSESSMENT = {
    "桥架": {
        "primary": "(2)+(1)", "regression": "宽×高+厚度→单价",
        "summary": "✅ 可支撑", "match_cond": "规格+材质+防火",
        "diff_explain": "厚度/表面处理",
    },
    "阀门": {
        "primary": "(1)+(2)", "regression": "DN→单价(按子类)",
        "summary": "✅ 可支撑", "match_cond": "DN+压力+阀体材质+连接",
        "diff_explain": "阀芯/密封材质",
    },
    "风口风阀": {
        "primary": "(2)+(1)", "regression": "面积+半周长→单价",
        "summary": "✅ 可支撑", "match_cond": "型号+规格+防火等级",
        "diff_explain": "钢板厚度/执行机构",
    },
    "母线槽": {
        "primary": "(2)+(1)", "regression": "额定电流→单价(非线性)",
        "summary": "✅ 可支撑", "match_cond": "母线类型+电流+防护等级",
        "diff_explain": "铜牌厚度/外壳材质",
    },
    "配电箱": {
        "primary": "(1)仅限", "regression": "BOM定价，不适用",
        "summary": "⚠️ 无品牌", "match_cond": "回路配置+元器件规格",
        "diff_explain": "元器件品牌",
    },
    "不锈钢管": {
        "primary": "(2)+(1)", "regression": "DN+壁厚→单价",
        "summary": "✅ 可支撑", "match_cond": "DN+壁厚+材质+连接",
        "diff_explain": "承压等级",
    },
    "水箱": {
        "primary": "暂不可", "regression": "需补充数据",
        "summary": "⚠️ 数据不足", "match_cond": "容积+材质等级",
        "diff_explain": "板厚/附件配置",
    },
    "潜水泵": {
        "primary": "(2)+(1)", "regression": "功率→单价",
        "summary": "✅ 可支撑", "match_cond": "流量+扬程+功率",
        "diff_explain": "泵体材质/控制方式",
    },
    "风机盘管": {
        "primary": "(1)+(2)", "regression": "风量→单价(数据偏少)",
        "summary": "✅ 可支撑", "match_cond": "管制+风量",
        "diff_explain": "过滤网/回风箱/滴水盘",
    },
    "空调泵": {
        "primary": "(2)+(1)", "regression": "功率→单价",
        "summary": "✅ 可支撑", "match_cond": "流量+扬程+功率",
        "diff_explain": "泵体/叶轮/轴材质",
    },
}

# ---------------------------------------------------------------------------
# 5. Report generation
# ---------------------------------------------------------------------------

def generate_report(results: list[dict]) -> str:
    lines = [
        "# 数据分析报告",
        "",
        "> 基于 `docs/data/` 中10个品类的原始CSV数据，按 `docs/design/03-数据分析计划.md` 框架分析。",
        "",
        "## 0. 数据来源",
        "",
        "原始数据来自 `docs/现有资料/材料汇总/` 目录，共 **10个Excel文件、98个Sheet**。",
        "",
        "| 专业 | 品类 | 来源文件 | Sheet数 | 汇总行数 | 项目数 |",
        "|------|------|---------|--------|---------|-------|",
        "| 电气 | 桥架 | 0桥架报价单格式模板.xls | 14 | 2,047 | 13 |",
        "| 电气 | 母线槽 | 0母线报价单格式模板.xls | 8 | 140 | 7 |",
        "| 电气 | 配电箱 | 0配电箱.xlsx | 9 | —* | 9 |",
        "| 给排水 | 阀门 | 0阀门询价格式.xls | 16 | 917 | 15 |",
        "| 给排水 | 不锈钢管 | 0不锈钢管清单.xlsx | 7 | 581 | 5 |",
        "| 给排水 | 水箱 | 0水箱报价清单.xlsx | 7 | 15 | 4 |",
        "| 给排水 | 潜水泵 | 0潜水泵询价格式.xlsx | 8 | 618 | 7 |",
        "| 暖通 | 风口风阀 | 0风口风阀报价单格式.xls | 13 | 3,101 | 11 |",
        "| 暖通 | 风机盘管 | 0风盘报价单格式.xls | 10 | 93 | 8 |",
        "| 暖通 | 空调泵 | 0空调泵询价格式 .xlsx | 6 | 48 | 4 |",
        "",
        "**合计：3个专业、10个品类、10个Excel文件、98个Sheet、约7,560条汇总数据行。**",
        "",
        "> *配电箱为BOM结构（每个箱子由多个元器件组成），原始数据按元器件逐行列出而非按箱子汇总，因此无法简单计数\"汇总行数\"。实际提取到4,433个箱子级价格记录。",
        "",
        "每个Excel文件结构：第1个Sheet为多项目汇总表，其余Sheet为各项目的单独报价。",
        "数据经 `scripts/convert_excel_to_csv.py` 转换为98个CSV文件存放于 `docs/data/`，完整索引见 `docs/data/manifest.json`。",
        "",
    ]

    # ---- Section 1: Hierarchy ----
    lines.append("## 1. 物料分类建议")
    lines.append("")
    lines.append("> **分类依据说明**")
    lines.append(">")
    lines.append("> 当前大类（专业）划分沿用客户数据文件的组织方式，子类及规格从实际数据中提取归纳。")
    lines.append("> 国标 GB/T50531-2009《建设工程计价设备材料划分标准》可作为参考基线，但该标准侧重造价计价时的设备/材料划分，并非施工采购领域的唯一分类依据。")
    lines.append("> 施工招标采购更常参考：GB50500 清单计价规范、各省市定额材料信息价分类、企业内部材料编码体系等。")
    lines.append("> **建议正式系统的分类体系以上海建工一建内部材料编码为准，国标作为兜底参考。**")
    lines.append("")
    lines.append("从实际数据中提取的分类层级（3个专业、10个品类）：")
    lines.append("")
    lines.append("```")
    for r in results:
        cat = r["category"]
        profession = PROFESSION_MAP.get(cat, "未分类")
        lines.append(f"{profession} > {cat}")
        for subcat, count in sorted(r["subcat_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"    ├── {subcat} ({count}条)")
    lines.append("```")
    lines.append("")

    # ---- Section 2: Attribute recommendations (ERP three-layer) ----
    lines.append("## 2. 物料属性建议")
    lines.append("")
    lines.append("> **属性分层说明**")
    lines.append(">")
    lines.append("> 参考行业通用的物料主数据管理思路，将比价属性分为三层：")
    lines.append(">")
    lines.append("> | 层级 | 作用 | 适用范围 |")
    lines.append("> |------|------|---------|")
    lines.append("> | **Layer 1 — 基础属性** | 这是什么：物料标识与分类 | 所有物料共享 |")
    lines.append("> | **Layer 2 — 扩展属性** | 能不能比：比价的前置匹配条件 | 按品类动态不同 |")
    lines.append("> | **Layer 3 — 商务属性** | 怎么比：价格对比与比价权重 | 所有物料共享 |")
    lines.append(">")
    lines.append("> 其中Layer 1和Layer 3对所有品类通用（下方只列一次），Layer 2按品类分别列出。")
    lines.append("")
    lines.append("### Layer 1 — 基础属性（所有品类共享）")
    lines.append("")
    lines.append("| 属性 | 说明 |")
    lines.append("|------|------|")
    lines.append("| 物料编码 | 系统内唯一标识 |")
    lines.append("| 物料名称 | 标准化品名 |")
    lines.append("| 规格型号 | 关键区分描述 |")
    lines.append("| 专业(大类) | 电气/给排水/暖通 |")
    lines.append("| 品类(小类) | 桥架/阀门/风口风阀等 |")
    lines.append("| 子类 | 闸阀/蝶阀/止回阀等 |")
    lines.append("| 基本计量单位 | m/台/套/座等 |")
    lines.append("| 品牌/制造商 | 供应商品牌 |")
    lines.append("| 执行标准 | 国标/行标/企标编号 |")
    lines.append("")
    lines.append("### Layer 2 — 扩展属性（按品类动态）")
    lines.append("")
    lines.append("> 以下为每个品类的扩展属性。标注 **[匹配]** 的属性表示\"必须相同才可比\"，标注 **[差异]** 的属性表示\"允许不同但需说明\"。")
    lines.append("")
    for cat, params in LAYER3_TECH_PARAMS.items():
        lines.append(f"#### {cat}")
        lines.append("")
        lines.append("| 扩展属性 | 数据中的来源列 | 比价规则 |")
        lines.append("|----------|--------------|---------|")
        for name, source, rule in params:
            tag = "**[匹配]**" if rule == "匹配" else "**[差异]**"
            lines.append(f"| {name} | {source} | {tag} |")
        lines.append("")
    lines.append("### Layer 3 — 商务属性（所有品类共享）")
    lines.append("")
    lines.append("> **属性定义策略：只收录原始数据中有独立列的字段。** 没有独立列的属性不定义为物料主数据字段，避免大量缺失或人工补录。")
    lines.append("")
    lines.append("| 属性 | 说明 | 比价作用 | 数据来源 |")
    lines.append("|------|------|---------|---------|")
    lines.append("| 不含税单价 | 比价核心指标 | 价格对比基准 | 各品类独立列 |")
    lines.append("| 含税单价 | 含增值税价格 | 合同总价计算 | 各品类独立列 |")
    lines.append("| 税率 | 通常13% | 含税/不含税换算 | 各品类独立列 |")
    lines.append("| 数量 | 工程量 | 总价计算 | 各品类独立列 |")
    lines.append("| 备注 | 原文保留，不拆分 | LLM比价分析时读取 | 各品类独立列 |")
    lines.append("")
    lines.append("> **备注列的处理策略**")
    lines.append(">")
    lines.append("> 分析原始报价单发现，备注列中包含以下比价相关信息：")
    lines.append(">")
    lines.append("> | 信息类型 | 出现频率 | 典型内容 |")
    lines.append("> |----------|---------|---------|")
    lines.append("> | 报价日期 | 高 | 2025.12.10、2025.11.3 |")
    lines.append("> | 付款条件（付款节点与比例） | 高 | 货到现场付70%→安装完毕付85%→竣工验收付95% |")
    lines.append("> | 预付款要求 | 高 | 30%预付款 / 无预付款 |")
    lines.append("> | 质保金比例及年限 | 高 | 5%质保2年、3%质保2年 |")
    lines.append("> | 支付方式 | 中 | 商票、区块链、12个月信用证 |")
    lines.append("> | 价格基准（母线槽特有） | 低 | 电解铜价参考：80870元/吨 |")
    lines.append("> | 技术补充（阀门） | 中 | 青铜丝扣暗杆闸阀、不锈钢法兰连接 |")
    lines.append("> | 技术补充（风口风阀） | 中 | 半自动机构、全自动机构、不含执行器、不锈钢材质 |")
    lines.append("> | 技术补充（母线槽） | 低 | 含弯头/始端/连接器，按直线段延长米计算 |")
    lines.append(">")
    lines.append("> 典型备注原文：")
    lines.append(">")
    lines.append("> `2025.12.10，无预付款，货到现场验收合格后30天内付到到货款70%，安装调试完毕付至到货款85%，竣工验收合格后付至95%，5%质保两年`")
    lines.append(">")
    lines.append("> **这些信息不定义为物料主数据的结构化字段**，原因：")
    lines.append("> 1. 非所有供应商都提供，强制结构化会导致大量空值")
    lines.append("> 2. 表述不统一，正则难以穷举所有写法")
    lines.append("> 3. 商务信息与技术补充混合在同一字段中")
    lines.append(">")
    lines.append("> **系统设计建议：** 备注原文作为独立字段完整保留。比价分析时由大语言模型（LLM）直接读取备注原文，完成以下工作：")
    lines.append("> 1. **信息提取** — 从非结构化文本中识别付款条件、质保期、支付方式等商务信息")
    lines.append("> 2. **横向对比** — 对多家供应商的商务条件进行对比分析（如付款节点差异、质保金比例差异）")
    lines.append("> 3. **缺失提醒** — 对未提供关键商务信息的供应商标注提醒，建议询价时补充确认")
    lines.append("> 4. **风险提示** — 识别异常条款（如质保金过低、付款条件过于苛刻等）并给出建议")
    lines.append("> 5. **技术补充归类** — 将备注中的技术信息（材质、机构类型等）关联到Layer 2扩展属性，辅助判断物料是否可比")
    lines.append("")

    # ---- Section 3: Price distribution ----
    lines.append("## 3. 价格分布分析")
    lines.append("")
    lines.append("> **指标说明**")
    lines.append(">")
    lines.append("> | 指标 | 含义 |")
    lines.append("> |------|------|")
    lines.append("> | CV（变异系数） | 标准差÷均价，反映价格离散程度。CV<0.5表示价格集中，CV>1.0表示价格分散（通常因规格跨度大或报价口径不统一） |")
    lines.append("> | P10 / P90 | 第10百分位和第90百分位价格，排除最低10%和最高10%的极端值后的价格区间 |")
    lines.append("> | 建议容差 | = CV × 1.5（下限5%），表示该子类价格的合理浮动范围，用于后续异常报价判定 |")
    lines.append(">")
    lines.append("> 以下价格统计按子类汇总，**未控制规格差异**（如同为\"闸阀\"但DN50与DN300价格天然不同）。因此CV偏高属正常现象，实际比价时会在Layer 2 [匹配] 条件下按具体规格分组后重新计算。")
    lines.append("")
    for r in results:
        cat = r["category"]
        lines.append(f"### {cat}")
        lines.append("")
        if not r["price_stats"]:
            lines.append("无有效价格数据。")
            lines.append("")
            continue
        lines.append("| 子类 | 数据量 | 均价 | 中位价 | 标准差 | CV | 最低 | 最高 | P10 | P90 | 建议容差 |")
        lines.append("|------|--------|------|--------|--------|------|------|------|-----|-----|---------|")
        for s in r["price_stats"]:
            lines.append(
                f"| {s['子类']} | {s['count']} | {s['mean']:.0f} | {s['median']:.0f} | "
                f"{s['std']:.0f} | {s['cv']:.2f} | {s['min']:.0f} | {s['max']:.0f} | "
                f"{s['p10']:.0f} | {s['p90']:.0f} | ±{s['suggested_threshold']:.0%} |"
            )
        if cat == "配电箱":
            lines.append("> **注意**：配电箱为BOM组合定价，每个箱子的价格由内部元器件清单决定，不同配置的箱子价格可相差数百倍。上表CV极高（4.45）属于结构性原因，不代表同配置产品报价离散。配电箱的比价应在相同回路配置+相同元器件规格下逐箱对比，上述统计仅供参考。")
            lines.append("")
        lines.append("")

    # ---- Section 4: Data sufficiency ----
    lines.append("## 4. 数据充分性分析")
    lines.append("")

    # ---- Section 4.1: Algorithm explanation ----
    lines.append("### 4.1 比价算法说明")
    lines.append("")
    lines.append("以下两种算法来自客户数据分析方案（原方案还包含\"原材料价格关联\"，因项目体量有限已移除）。")
    lines.append("")
    lines.append("**算法(1)：历史与现在比对**")
    lines.append("")
    lines.append("用同规格物料的历史成交价建立基线，新报价偏离基线则报警。")
    lines.append("")
    lines.append("- 收集同规格历史价格，计算四分位数 Q1、Q3，四分位距 IQR = Q3 − Q1")
    lines.append("- 合理区间：[Q1 − 1.5×IQR, Q3 + 1.5×IQR]，超出即报警")
    lines.append("")
    lines.append("> **举例**：桥架 200×100 热镀锌，在13个项目中历史单价（元/m）：45, 47, 48, 49, 50, 51, 52, 55")
    lines.append("> - Q1=47.5, Q3=51.5, IQR=4 → 合理区间 [41.5, 57.5]")
    lines.append("> - 新报价 38元 → 低于 41.5 → **异常偏低，报警**")
    lines.append("> - 新报价 53元 → 区间内 → 正常")
    lines.append("")
    lines.append("**适用范围**：同一规格在多个项目间反复出现的品类（桥架、阀门、风口风阀等），每组需≥30条历史记录。BOM定价品类（配电箱）只能做粗略参考。")
    lines.append("")
    lines.append("**算法(2)：价格与规格回归**")
    lines.append("")
    lines.append("用规格参数（尺寸、功率等）拟合价格预测模型，偏离预测值过大则报警。")
    lines.append("")
    lines.append("- 建立回归模型：单价 = f(规格参数)，线性或非线性")
    lines.append("- 修正Z分数 = 0.6745 × 残差 / MAD，Z > 3.0 判定为异常")
    lines.append("")
    lines.append("> **举例**：风口风阀（防火阀），规格为长×宽 (mm)，转化为面积+半周长后线性回归")
    lines.append("> - 模型：`单价 = a×面积 + b×半周长 + c`")
    lines.append("> - 800×400 防火阀报价 1,200元，模型预测 850元，残差 = 350")
    lines.append("> - 修正Z分数 = 3.8 > 3.0 → **异常偏高，报警**")
    lines.append("")
    lines.append("**适用范围**：规格参数为数值型且与价格有函数关系的品类，需≥50条数据。BOM定价品类不适用（价格由元器件清单决定）。")
    lines.append("")
    lines.append("**比价流程**")
    lines.append("")
    lines.append("每个品类的比价分三步：")
    lines.append("")
    lines.append("```")
    lines.append("① 可比性判定 — Layer 2 扩展属性中标记为 [匹配] 的条件必须完全一致，才能放在一起比")
    lines.append("② 价格分析 — 选择合适的算法：有规格→价格关系的用(2)回归，BOM定价的用(1)历史比对")
    lines.append("③ 差异解释 — 价格有偏差时，用 [差异] 属性和备注中的商务条件（LLM提取）解释原因")
    lines.append("```")
    lines.append("")

    # ---- Section 4.2: Single comprehensive table ----
    lines.append("### 4.2 各品类比价建议")
    lines.append("")
    lines.append("| 品类 | 总评 | 数据(条/项目/供应商) | 建议算法 | 回归模型 | [匹配]条件 | [差异]解释 | 主要品牌 |")
    lines.append("|------|------|---------------------|---------|---------|-----------|-----------|---------|")
    for r in results:
        cat = r["category"]
        a = ALGO_ASSESSMENT.get(cat, {})
        data_col = f"{r['n_valid_price']}/{r['n_projects']}/{r['n_brands']}"
        pct = float(r["price_missing_pct"])
        if pct > 1:
            data_col += f" (缺失{r['price_missing_pct']}%)"
        if r["n_brands"] > 0 and r["brands"]:
            top3 = r["brands"][:3]
            if r["n_brands"] > 3:
                brand_col = ", ".join(top3) + f"等{r['n_brands']}家"
            else:
                brand_col = ", ".join(top3)
        else:
            brand_col = "**无，需补充**"
        lines.append(
            f"| {cat} | {a.get('summary', '—')} | {data_col} "
            f"| {a.get('primary', '—')} | {a.get('regression', '—')} "
            f"| {a.get('match_cond', '—')} | {a.get('diff_explain', '—')} "
            f"| {brand_col} |"
        )
    lines.append("")

    # ---- Section 5: Pending confirmations (merged with data supplement) ----
    lines.append("## 5. 待确认事项")
    lines.append("")
    lines.append("以下事项需与客户确认后方可进入系统设计阶段，每项均给出建议方案。")
    lines.append("")
    lines.append("| # | 确认事项 | 背景 | 建议方案 |")
    lines.append("|---|---------|------|---------|")
    lines.append("| 1 | **物料分类体系** | 当前大类/品类/子类沿用数据文件结构，不一定与一建内部编码一致 | 以一建内部材料编码为准，国标GB/T50531作为兜底参考 |")
    lines.append("| 2 | **配电箱比价粒度** | 配电箱为BOM组合定价，当前无多供应商数据 | 按\"相同回路配置+相同元器件规格\"逐箱比价；需补充≥3家供应商报价 |")
    lines.append("| 3 | **配电箱供应商数据补充** | 9个项目均无品牌/供应商信息，无法横向比价 | 优先补充配电箱的多供应商报价数据 |")
    lines.append("| 4 | **水箱数据补充** | 仅14条有效价格，统计意义不足 | 补充更多项目水箱报价（目标≥50条），否则暂不启用统计报警 |")
    lines.append("| 5 | **风机盘管供应商补充** | 仅3家（新晃、特灵、格力），回归数据偏少 | 补充1-2家供应商；按管制+风量分组后单组数据不足以建立高置信度回归 |")
    lines.append("| 6 | **空调泵历史数据补充** | 46条有效价格、4个项目，规格分散后每组数据有限 | 补充更多项目数据以稳定历史比对基线 |")
    lines.append("| 7 | **Layer 2 扩展属性确认** | 各品类的[匹配]/[差异]划分直接决定\"哪些报价可以放在一起比\" | 请各专业负责人审核Section 2中各品类的扩展属性表，确认匹配/差异标注是否合理 |")
    lines.append("| 8 | **品牌作为比价维度的权重** | 客户希望品牌参与权重比价，但结构化数据中仅品牌可用作权重维度 | 方案A：品牌仅作为筛选条件（指定品牌或同档次品牌才可比）；方案B：品牌参与加权评分（需定义品牌档次映射表） |")
    lines.append("| 9 | **备注信息提取范围** | 备注中含付款条件、质保、技术补充等，LLM可提取但需明确优先级 | 首轮仅提取付款条件和质保信息用于比价报告展示，技术补充归类到Layer 2辅助判断 |")
    lines.append("| 10 | **比价算法选择确认** | Section 4.1已给出各品类建议算法，需确认是否采纳 | 按Section 4.1建议执行：有回归条件的品类用规格回归+历史比对，BOM定价类用历史比对 |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_analysis() -> list[dict]:
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    parsers = {
        "桥架": ("桥架报价单格式模板_汇总.csv", parse_桥架),
        "阀门": ("阀门询价格式_汇总.csv", parse_阀门),
        "风口风阀": ("风口风阀报价单格式_汇总.csv", parse_风口风阀),
        "母线槽": ("母线报价单格式模板_汇总.csv", parse_母线),
        "不锈钢管": ("不锈钢管清单_汇总.csv", parse_不锈钢管),
        "水箱": ("水箱报价清单_汇总.csv", parse_水箱),
        "潜水泵": ("潜水泵询价格式_汇总.csv", parse_潜水泵),
        "风机盘管": ("风盘报价单格式_汇总.csv", parse_风机盘管),
        "空调泵": ("空调泵询价格式_汇总.csv", parse_空调泵),
    }

    results = []
    for category, (csv_name, parser) in parsers.items():
        csv_path = DATA_DIR / csv_name
        if not csv_path.exists():
            print(f"[SKIP] {csv_name} not found")
            continue
        print(f"[分析] {category} ...")
        try:
            df = parser(csv_path)
            entries = [e for e in manifest if e["category"] == category]
            r = analyze_category(df, category, entries)
            results.append(r)
            print(f"  有效数据: {r['n_valid_price']}/{r['n_total']}, 子类: {len(r['subcat_counts'])}, 品牌: {r['n_brands']}")
        except Exception as e:
            print(f"[ERROR] {category}: {e}")
            import traceback; traceback.print_exc()

    # 配电箱 special handling
    print("[分析] 配电箱 ...")
    try:
        df_pdx = parse_配电箱_all(manifest)
        if len(df_pdx) > 0:
            df_pdx["子类"] = df_pdx["名称"].apply(lambda x: classify_subcat(x, "配电箱"))
            entries = [e for e in manifest if e["category"] == "配电箱"]
            n_projects = len(entries)
            prices = df_pdx["单价"].dropna()
            r = {
                "category": "配电箱",
                "n_total": len(df_pdx),
                "n_valid_price": len(prices[prices > 0]),
                "price_missing_pct": round((len(df_pdx) - len(prices[prices > 0])) / max(len(df_pdx), 1) * 100, 1),
                "n_projects": n_projects,
                "brands": [],
                "n_brands": 0,
                "subcat_counts": df_pdx["子类"].value_counts().to_dict(),
                "price_stats": [],
                "dates": [],
            }
            # price stats per subcat
            for subcat, group in df_pdx.groupby("子类"):
                p = group["单价"].dropna()
                p = p[p > 0]
                if len(p) >= 1:
                    r["price_stats"].append({
                        "子类": subcat,
                        "count": len(p),
                        "mean": round(p.mean(), 2),
                        "median": round(p.median(), 2),
                        "std": round(p.std(), 2) if len(p) > 1 else 0,
                        "cv": round(p.std() / p.mean(), 3) if len(p) > 1 and p.mean() > 0 else 0,
                        "min": round(p.min(), 2),
                        "max": round(p.max(), 2),
                        "p10": round(np.percentile(p, 10), 2) if len(p) >= 5 else round(p.min(), 2),
                        "p90": round(np.percentile(p, 90), 2) if len(p) >= 5 else round(p.max(), 2),
                        "suggested_threshold": round(max(p.std() / p.mean() * 1.5 if len(p) > 1 and p.mean() > 0 else 0, 0.05), 3),
                    })
            r["sufficiency"] = assess_sufficiency(r)
            results.append(r)
            print(f"  箱子总数: {len(df_pdx)}, 有效价格: {r['n_valid_price']}")
    except Exception as e:
        print(f"[ERROR] 配电箱: {e}")
        import traceback; traceback.print_exc()

    # sort results by profession
    order = ["桥架", "母线槽", "配电箱", "阀门", "不锈钢管", "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵"]
    results.sort(key=lambda x: order.index(x["category"]) if x["category"] in order else 99)
    return results


def main():
    results = run_analysis()

    report = generate_report(results)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\n报告已写入: {REPORT_PATH}")

    for r in results:
        if r["price_stats"]:
            stats_df = pd.DataFrame(r["price_stats"])
            stats_df.to_csv(OUT_DIR / f"{r['category']}_价格统计.csv", index=False, encoding="utf-8-sig")

    print(f"统计CSV已写入: {OUT_DIR}")


if __name__ == "__main__":
    main()
