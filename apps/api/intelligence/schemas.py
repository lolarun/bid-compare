"""JSON Schema definitions for structured extraction targets.

Used by:
- ExtractionPipeline to guide the LLM via prompt suffix
- post-processors to validate / coerce fields

Two top-level schemas:
- TENDER_SCHEMA: 招标文件 (tender document) → project info + material list
- QUOTE_SCHEMA: 供应商报价单 (supplier quote) → priced line items
"""

from typing import Any


TENDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_name", "items"],
    "properties": {
        "project_name": {"type": "string", "description": "项目名称"},
        "project_code": {"type": "string", "description": "招标编号"},
        "tender_date": {"type": "string", "description": "招标发布日期 (YYYY-MM-DD)"},
        "deadline": {"type": "string", "description": "投标截止日期 (YYYY-MM-DD)"},
        "items": {
            "type": "array",
            "description": "采购材料清单",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "材料名称"},
                    "category": {"type": "string", "description": "品类 (桥架/阀门/...)"},
                    "spec": {"type": "string", "description": "规格型号"},
                    "unit": {"type": "string", "description": "单位"},
                    "quantity": {
                        "type": ["number", "null"],
                        "description": "数量；'若干'/'按图'等留 null",
                    },
                    "remark": {"type": "string", "description": "备注/技术要求"},
                    "extended_attrs": {
                        "type": "object",
                        "description": "品类专属技术参数（桥架板厚、阀门压力等），无则留空对象 {}",
                        "additionalProperties": True,
                    },
                },
            },
        },
    },
}


QUOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "supplier_name": {"type": "string", "description": "供应商/投标单位名称"},
        "quote_date": {"type": "string", "description": "报价日期 (YYYY-MM-DD)"},
        "items": {
            "type": "array",
            "description": "报价明细，每行一种材料",
            "items": {
                "type": "object",
                "required": ["material"],
                "properties": {
                    "material": {"type": "string", "description": "材料名称"},
                    "spec": {"type": "string", "description": "规格型号"},
                    "brand": {"type": "string", "description": "品牌/厂家"},
                    "unit": {"type": "string", "description": "单位"},
                    "qty": {"type": ["number", "null"], "description": "数量"},
                    "unit_price": {
                        "type": ["number", "null"],
                        "description": "含税单价（元）",
                    },
                    "unit_price_excl_tax": {
                        "type": ["number", "null"],
                        "description": "不含税单价（元）",
                    },
                    "total_price": {"type": ["number", "null"], "description": "总价"},
                    "tax_rate": {
                        "type": ["number", "null"],
                        "description": "税率（如 0.13 = 13%）",
                    },
                    "material_type": {
                        "type": "string",
                        "description": "材质（不锈钢/球墨铸铁/碳钢/黄铜等），无则留空",
                    },
                    "remark": {
                        "type": "string",
                        "description": "备注（付款条款、保修期等关键条款摘要）",
                    },
                    "normalized_material": {
                        "type": "string",
                        "description": "OCR纠错后的材料名称（仅当发现明显形近字OCR错误时填写，否则留空字符串）",
                    },
                    "ocr_correction_reason": {
                        "type": "string",
                        "description": "OCR纠错依据（词表命中+相邻行DN/PN连续性等），无纠错时留空字符串",
                    },
                    "canonical": {
                        "type": "object",
                        "description": "阀门类结构化技术参数（非阀门品类留空 {}）",
                        "properties": {
                            "valve_type": {"type": "string", "description": "阀门类型"},
                            "dn": {"type": "string", "description": "公称直径，如 DN25"},
                            "pn": {"type": "string", "description": "公称压力，如 PN16"},
                            "material": {"type": "string", "description": "主材质"},
                            "connection": {"type": "string", "description": "连接方式"},
                        },
                    },
                },
            },
        },
    },
}


META_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string", "description": "投标单位/供应商全称"},
        "bid_total": {"type": ["number", "null"], "description": "投标总价（数字）"},
        "bid_total_basis": {
            "type": "string",
            "description": "总价口径: tax_included | tax_excluded | unknown",
        },
        "tax_rate": {"type": ["number", "null"], "description": "税率小数，如 0.13"},
    },
}


SCHEMAS_BY_TYPE: dict[str, dict[str, Any]] = {
    "tender": TENDER_SCHEMA,
    "quote": QUOTE_SCHEMA,
}
