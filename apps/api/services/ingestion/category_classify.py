"""品类分类器 — 从采购项名称/规格推断 10 个物料品类之一。

解决"专业(给排水)被误当品类"的根因：detected_category 必须来自采购项
**内容**(品名/规格)的品类识别，而不是 Excel 的专业列。

10 个品类(与 apps/api/core/config.py ALL_CATEGORIES 一致)：
    桥架、母线槽、配电箱、阀门、不锈钢管、水箱、潜水泵、风口风阀、风机盘管、空调泵

设计：纯函数，无 I/O。关键词表按"更具体的品类优先"排序，规避歧义：
  - 风口风阀(风阀/防火阀…含"阀")必须在通用"阀门"之前判定。
  - 母线槽(含"线槽")必须在桥架(线槽)之前判定。
  - 水箱(不锈钢水箱)必须在不锈钢管之前判定。
阀门作为给排水阀类兜底，并复用 valve canonical 词表补漏(如倒流防止器/Y型过滤器)。
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.api.services.ingestion.canonical import extract_valve_canonical
from apps.api.services.ingestion.standardize import standardize_name

# 11 个合法品类
ALL_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "电缆", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]

# 低于此置信度视为 unknown，前端要求人工选择
CONFIDENCE_THRESHOLD = 0.6

# 关键词表 —— 顺序即优先级(更具体/易歧义的品类在前)。
# 每条 (品类, [关键词])；命中即归类，越靠前越优先。
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    # —— 暖通风系统：必须排在"阀门"之前(风阀/防火阀含"阀") ——
    ("风口风阀", ["风口风阀", "风量调节阀", "防火调节阀", "防火阀", "排烟阀", "送风口",
                "回风口", "排风口", "新风口", "散流器", "百叶", "风阀", "风口"]),
    ("风机盘管", ["风机盘管", "盘管", "fcu"]),
    ("空调泵", ["空调泵", "冷冻水泵", "冷却水泵", "冷冻泵", "冷却泵", "空调水泵",
              "冷热水循环泵", "热水循环泵", "循环泵"]),
    # —— 给排水泵：潜水泵(与空调泵区分) ——
    ("潜水泵", ["潜水排污泵", "潜水泵", "潜污泵", "排污泵", "污水泵", "提升泵", "潜水"]),
    # —— 电气 ——
    ("母线槽", ["母线槽", "插接母线", "母线"]),
    ("桥架", ["电缆桥架", "梯式桥架", "托盘桥架", "槽式桥架", "桥架", "线槽"]),
    ("配电箱", ["配电箱", "配电柜", "开关柜", "开关箱", "控制箱", "照明箱",
              "动力箱", "计量箱", "电表箱", "双电源"]),
    # —— 电缆：必须排在桥架之后("电缆桥架"是桥架不是电缆)。招标清单常只给
    #    型号串(如 RTTYZ-3*240+2*120)，无中文品名，故需覆盖型号前缀。 ——
    ("电缆", ["矿物绝缘电缆", "矿物电缆", "电力电缆", "控制电缆", "预分支电缆",
            "电缆", "电线",
            "BTTZ", "BTTVZ", "BTLY", "BBTRZ", "YTTW",
            "RTTZ", "RTTYZ", "RTTVZ", "RTXMY",
            "YJV", "YJY", "YJLV", "WDZA", "WDZ", "NG-A",
            "NH-YJ", "ZR-YJ", "ZC-YJ", "RVV", "BVR",
            # 通信/控制电缆
            "HYA", "HYAT", "RVVP", "KVV", "ZR-KVV"]),
    # —— 给排水设备/管材：水箱在不锈钢管之前(不锈钢水箱) ——
    ("水箱", ["不锈钢水箱", "膨胀水箱", "消防水箱", "生活水箱", "储水箱", "水箱"]),
    ("不锈钢管", ["薄壁不锈钢管", "不锈钢水管", "不锈钢钢管", "不锈钢管", "卡压管", "沟槽管"]),
    # —— 阀门：给排水阀类兜底，"阀"放最后避免误吞风阀 ——
    ("阀门", ["倒流防止器", "真空破坏器", "水锤消除器", "Y型过滤器", "过滤器",
            "减压阀组", "电动蝶阀", "橡胶瓣止回阀", "截止阀", "止回阀", "闸阀",
            "球阀", "蝶阀", "减压阀", "疏水阀", "安全阀", "调节阀", "电磁阀",
            "排气阀", "底阀", "取水阀", "阀"]),
]


@dataclass
class CategoryGuess:
    """品类识别结果。category 为空字符串表示 unknown(需人工确认)。"""

    category: str           # 10 品类之一，或 "" 表示 unknown
    confidence: float       # 0.0–1.0
    reason: str             # 识别依据(命中关键词/canonical/无匹配)

    @property
    def is_unknown(self) -> bool:
        return not self.category or self.confidence < CONFIDENCE_THRESHOLD


def classify_category(
    name: str, spec: str = "", pressure: str = "", material: str = "",
) -> CategoryGuess:
    """从采购项名称/规格推断品类。

    Returns CategoryGuess(category, confidence, reason)。
    识别不到或低置信 → category="" (unknown)，由调用方/前端要求人工选择。
    """
    raw = " ".join(x for x in [name, spec, pressure, material] if x).strip()
    if not raw:
        return CategoryGuess("", 0.0, "采购项名称为空")

    # 归一化(Φ57→DN50、繁简、全半角等)，并保留原文一并扫描
    normalized = standardize_name(raw)["standardized"]
    haystacks = (raw, normalized)

    for cat, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            kwl = kw.lower()
            if any(kwl in h.lower() for h in haystacks):
                # 单字"阀"置信度略低(通用兜底)，其余具体关键词高置信
                conf = 0.85 if kw == "阀" else 0.95
                return CategoryGuess(cat, conf, f"命中关键词「{kw}」→{cat}")

    # 兜底：valve canonical(覆盖词表未列全的阀类，如未来新增阀型)
    canon = extract_valve_canonical(name, spec, pressure, material)
    if canon.get("valve_type"):
        return CategoryGuess(
            "阀门", 0.8, f"valve canonical 命中「{canon['valve_type']}」→阀门"
        )

    return CategoryGuess("", 0.0, "无匹配关键词，需人工确认")


def classify_breakdown(
    items: list[dict],
) -> tuple[dict[str, int], str, bool, int]:
    """对一批采购项做品类分布统计。

    items: [{"name","spec","pressure","materials"...}]，逐项 classify。
    Returns:
        (breakdown, detected_category, has_multiple, unknown_count)
        - breakdown: {品类: 数量}(不含 unknown)
        - detected_category: 多数派品类(breakdown 为空时为 "")
        - has_multiple: 是否跨多个品类
        - unknown_count: 识别不到品类的项数
    """
    breakdown: dict[str, int] = {}
    unknown_count = 0
    for it in items:
        mats = it.get("materials") or {}
        material_text = "/".join(str(v) for v in mats.values()) if isinstance(mats, dict) else str(mats or "")
        g = classify_category(
            str(it.get("name") or ""),
            str(it.get("spec") or ""),
            str(it.get("pressure") or ""),
            material_text,
        )
        if g.is_unknown:
            unknown_count += 1
        else:
            breakdown[g.category] = breakdown.get(g.category, 0) + 1

    detected = max(breakdown, key=lambda k: breakdown[k]) if breakdown else ""
    has_multiple = len(breakdown) > 1
    return breakdown, detected, has_multiple, unknown_count
