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

    # LLM / Intelligence
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_VISION_MODEL: str = "qwen3-vl-plus"
    LLM_VISION_MODEL_FALLBACK: str = "qwen-vl-max"
    LLM_PROVIDER: str = "auto"  # 'auto' | 'qwen_vl' | 'mock'

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
    "桥架": "BRG", "母线槽": "BUS", "配电箱": "PDB",
    "阀门": "VLV", "不锈钢管": "SSP", "水箱": "WTK", "潜水泵": "SMP",
    "风口风阀": "FAV", "风机盘管": "FCU", "空调泵": "ACP",
}

PROFESSION_MAP = {
    "桥架": "电气", "母线槽": "电气", "配电箱": "电气",
    "阀门": "给排水", "不锈钢管": "给排水", "水箱": "给排水", "潜水泵": "给排水",
    "风口风阀": "暖通", "风机盘管": "暖通", "空调泵": "暖通",
}

ALL_CATEGORIES = list(PROFESSION_MAP.keys())

# ─── Default scoring weights (A层 5维模型) ──────────────────────────────────

DEFAULT_SCORING_WEIGHTS = {
    "price":        0.40,
    "history":      0.20,
    "completeness": 0.15,
    "brand":        0.15,
    "commercial":   0.10,
}

# ─── Default alert thresholds (B层 各品类) ──────────────────────────────────

DEFAULT_THRESHOLDS = {
    "default":  {"yellow": 0.05, "red": 0.10},
    "桥架":     {"yellow": 0.08, "red": 0.15},
    "母线槽":   {"yellow": 0.06, "red": 0.12},
    "配电箱":   {"yellow": 0.08, "red": 0.15},
    "阀门":     {"yellow": 0.06, "red": 0.12},
    "不锈钢管": {"yellow": 0.05, "red": 0.10},
    "水箱":     {"yellow": 0.08, "red": 0.15},
    "潜水泵":   {"yellow": 0.06, "red": 0.12},
    "风口风阀": {"yellow": 0.07, "red": 0.13},
    "风机盘管": {"yellow": 0.07, "red": 0.13},
    "空调泵":   {"yellow": 0.06, "red": 0.12},
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
