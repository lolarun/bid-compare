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
                    "remark": {
                        "type": "string",
                        "description": "备注（付款条款、保修期等关键条款摘要）",
                    },
                },
            },
        },
    },
}


SCHEMAS_BY_TYPE: dict[str, dict[str, Any]] = {
    "tender": TENDER_SCHEMA,
    "quote": QUOTE_SCHEMA,
}
