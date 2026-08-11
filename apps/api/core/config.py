"""Application constants and default configuration."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── Runtime settings (loaded from env / .env) ──────────────────────────────


class Settings(BaseSettings):
    """Environment-driven runtime configuration.

    Loaded from `apps/api/.env` (gitignored) or the process environment.
    See `apps/api/.env.example` for the template.
    """

    # LLM / Intelligence — DashScope (Alibaba Cloud)
    DASHSCOPE_API_KEY: str = ""
    # Comma-separated list of keys for multi-key rotation (takes priority over single key)
    DASHSCOPE_API_KEYS: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_PROVIDER: str = "dashscope_ocr"  # 'dashscope_ocr' | 'mock'

    # Two-stage OCR + LLM pipeline models
    DASHSCOPE_OCR_MODEL: str = "qwen-vl-ocr-latest"
    DASHSCOPE_LLM_MODEL: str = "qwen3.6-flash"

    # ── 报价识别 ────────────────────────────────────────────────────────────
    # 报价走 VL-direct（整份页面图像 → 视觉模型 → CSV）。legacy 的报价分支已归档，
    # 故**没有 QUOTE_RECOGNIZER 开关**——留着它就等于留着"这次是哪条路"的疑问。
    # 招标清单仍走 legacy（services/tender_pdf.py），见 docs/design/21 Phase 2。
    #
    # **不复用 DASHSCOPE_LLM_MODEL**：那是通用文本模型，改它会影响其它调用方。
    DASHSCOPE_QUOTE_VL_MODEL: str = "qwen3.7-plus"
    # 方向预检模型。判定的是"这页要不要转"，与抽取分开配置。
    DASHSCOPE_QUOTE_ORIENT_MODEL: str = "qwen3.7-plus"
    # 方向预检投票轮数。实测单轮不稳（同份同配置跑出 3/10、10/10、10/10）；
    # 但投票只降低崩塌概率、不消除，无过半共识的页一律不转并标 REVIEW。
    QUOTE_ORIENT_VOTES: int = 3

    # OCR PDF render quality (Layer 0). Higher = clearer small-font scanned tables
    # (reduces 形近字 OCR errors like 闸阀→阀阀, 橡胶瓣→橡胶海). Defaults preserve
    # prior behavior (2.0 / 2400); raise via env after A/B confirms improvement.
    OCR_RENDER_SCALE: float = 2.0
    OCR_MAX_EDGE_PX: int = 2400

    # File storage
    UPLOAD_DIR: str = "data/uploads"

    # CORS (parsed elsewhere)
    CORS_ORIGINS: str = (
        "http://localhost:3000,http://localhost:5173,"
        "http://127.0.0.1:3000,http://127.0.0.1:5173"
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton getter for app settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


# ─── Profession / Category constants ────────────────────────────────────────

PROFESSION_ABBR = {
    "电气": "EL", "给排水": "WS", "暖通": "HV", "消防": "FP",
    "智能化": "IT", "电梯": "EV", "幕墙": "CW", "其他": "OT",
}

CATEGORY_ABBR = {
    "桥架": "BRG", "母线槽": "BUS", "配电箱": "PDB", "电缆": "CBL",
    "阀门": "VLV", "不锈钢管": "SSP", "水箱": "WTK", "潜水泵": "SMP",
    "风口风阀": "FAV", "风机盘管": "FCU", "空调泵": "ACP",
}

# NOTE: key order matters — enhance._guess_category returns the first category
# whose name appears in the material name, so 桥架 must stay ahead of 电缆
# ("电缆桥架" is a tray, not a cable).
PROFESSION_MAP = {
    "桥架": "电气", "母线槽": "电气", "配电箱": "电气", "电缆": "电气",
    "阀门": "给排水", "不锈钢管": "给排水", "水箱": "给排水", "潜水泵": "给排水",
    "风口风阀": "暖通", "风机盘管": "暖通", "空调泵": "暖通",
}

ALL_CATEGORIES = list(PROFESSION_MAP.keys())

# ─── Default scoring weights (A层 5维模型) ──────────────────────────────────

DEFAULT_SCORING_WEIGHTS = {
    # Keys must match SettingsView.vue (long names) — scoring.py reads with same.
    # 2026-06-06: removed brand_compliance (品牌档位打分) per user — manual bid
    # comparison never scores by brand tier; its 0.15 redistributed to price/history/commercial.
    "price_competitiveness": 0.45,
    "history_cooperation":   0.25,
    "quote_completeness":    0.15,
    "commercial_terms":      0.15,
}

# ─── Default alert thresholds (B层 各品类) ──────────────────────────────────

DEFAULT_THRESHOLDS = {
    "default":  {"yellow": 0.05, "red": 0.10},
    "桥架":     {"yellow": 0.08, "red": 0.15},
    "母线槽":   {"yellow": 0.06, "red": 0.12},
    "配电箱":   {"yellow": 0.08, "red": 0.15},
    "电缆":     {"yellow": 0.05, "red": 0.10},
    "阀门":     {"yellow": 0.06, "red": 0.12},
    "不锈钢管": {"yellow": 0.05, "red": 0.10},
    "水箱":     {"yellow": 0.08, "red": 0.15},
    "潜水泵":   {"yellow": 0.06, "red": 0.12},
    "风口风阀": {"yellow": 0.07, "red": 0.13},
    "风机盘管": {"yellow": 0.07, "red": 0.13},
    "空调泵":   {"yellow": 0.06, "red": 0.12},
}

# Comparison policy is configuration, not a route-local category exception.
COMPARISON_PROFILE_BY_CATEGORY = {
    "配电箱": {
        "key": "panel_horizontal",
        "history_baseline": False,
        "review_hint": "以同一轮次整箱横向报价为主；历史数据仅供查阅，不参与基准偏差。",
    },
    # Cable unit prices are quoted against a declared base copper price and
    # adjusted by formula, so historical unit prices are not comparable across
    # rounds. Horizontal comparison only, and the base price must be checked.
    "电缆": {
        "key": "cable_horizontal",
        "history_baseline": False,
        "review_hint": "电缆单价按基准铜价报价并随铜价调差；仅做本轮横向比价，比价前须确认各家基准铜价一致。",
    },
}

# ─── Extended attribute schemas per category ────────────────────────────────

EXTENDED_ATTR_SCHEMAS: dict[str, list[dict]] = {
    "桥架": [
        {"key": "surface", "label": "表面处理", "source": "报价单/投标", "role": "匹配"},
        {"key": "thickness", "label": "板材厚度(mm)", "source": "报价单/投标", "role": "差异"},
        {"key": "load_type", "label": "荷载等级", "source": "投标/图纸", "role": "匹配"},
        {"key": "fire_rating", "label": "防火等级", "source": "投标/图纸", "role": "匹配"},
    ],
    "母线槽": [
        {"key": "rated_current", "label": "额定电流(A)", "source": "报价单/投标", "role": "匹配"},
        {"key": "ip_rating", "label": "防护等级", "source": "投标", "role": "匹配"},
        {"key": "conductor", "label": "导体材质", "source": "报价单", "role": "差异"},
        {"key": "insulation", "label": "绝缘方式", "source": "投标", "role": "差异"},
    ],
    "配电箱": [
        {"key": "circuit_count", "label": "回路数", "source": "图纸/BOM", "role": "匹配"},
        {"key": "breaker_brand", "label": "元器件品牌", "source": "报价单", "role": "差异"},
        {"key": "box_material", "label": "箱体材质", "source": "报价单/投标", "role": "差异"},
        {"key": "ip_rating", "label": "防护等级", "source": "投标", "role": "匹配"},
    ],
    "阀门": [
        {"key": "valve_type", "label": "阀门类型", "source": "报价单/图纸", "role": "匹配"},
        {"key": "pressure", "label": "公称压力(MPa)", "source": "报价单/投标", "role": "匹配"},
        {"key": "body_material", "label": "阀体材质", "source": "报价单", "role": "差异"},
        {"key": "connection", "label": "连接方式", "source": "报价单/图纸", "role": "匹配"},
    ],
    "不锈钢管": [
        {"key": "steel_grade", "label": "钢种牌号", "source": "报价单/投标", "role": "匹配"},
        {"key": "wall_thickness", "label": "壁厚(mm)", "source": "报价单", "role": "差异"},
        {"key": "connection", "label": "连接方式", "source": "报价单/图纸", "role": "匹配"},
    ],
    "水箱": [
        {"key": "tank_material", "label": "材质", "source": "报价单/投标", "role": "匹配"},
        {"key": "volume", "label": "容积(m³)", "source": "图纸/BOM", "role": "匹配"},
        {"key": "insulation", "label": "保温方式", "source": "投标", "role": "差异"},
    ],
    "潜水泵": [
        {"key": "flow_rate", "label": "流量(m³/h)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "head", "label": "扬程(m)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "power", "label": "功率(kW)", "source": "报价单", "role": "差异"},
        {"key": "material", "label": "过流部件材质", "source": "投标", "role": "差异"},
    ],
    "风口风阀": [
        {"key": "type", "label": "类型", "source": "报价单/图纸", "role": "匹配"},
        {"key": "material", "label": "材质", "source": "报价单/投标", "role": "差异"},
        {"key": "drive_type", "label": "驱动方式", "source": "投标", "role": "差异"},
    ],
    "风机盘管": [
        {"key": "cooling_cap", "label": "制冷量(kW)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "air_volume", "label": "风量(m³/h)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "install_type", "label": "安装方式", "source": "投标", "role": "匹配"},
        {"key": "coil_rows", "label": "盘管排数", "source": "投标", "role": "差异"},
    ],
    "空调泵": [
        {"key": "flow_rate", "label": "流量(m³/h)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "head", "label": "扬程(m)", "source": "报价单/图纸", "role": "匹配"},
        {"key": "power", "label": "功率(kW)", "source": "报价单", "role": "差异"},
        {"key": "pump_type", "label": "泵型", "source": "报价单/投标", "role": "匹配"},
    ],
}

# ─── Material naming standards per category ───────────────────────────────
# Used by standardize service for validation and structured decomposition.
# Source: 用户反馈 2026-05-27 naming reference tables.

NAMING_STANDARDS: dict[str, dict[str, list[str]]] = {
    "桥架": {
        "结构形式": ["槽式", "托盘式", "梯式", "网格式", "模压增强型", "波纹型"],
        "材质": ["钢质", "不锈钢", "铝合金", "玻璃钢", "高分子", "冷轧钢"],
        "表面处理": ["热浸镀锌", "热镀锌", "彩钢", "涂塑", "喷塑", "锌铝镁"],
        "特殊要求": ["防水", "防火", "带分隔板", "屏蔽"],
    },
    "风口风阀": {
        "名称": ["风口", "风阀", "防火阀", "静压箱", "消声器"],
        "材质": ["镀锌钢", "不锈钢", "铝合金"],
        "板材厚度": ["1.6mm", "1.8mm", "2.0mm"],
        "特殊要求": ["带门铰", "带防虫网", "带风箱", "带驱动", "带执行机构"],
    },
}
