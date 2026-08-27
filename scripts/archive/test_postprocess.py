"""Test _postprocess_tender with extended_attrs."""
import json
from apps.api.intelligence.pipeline import ExtractionPipeline

data = {
    "project_name": "Test Project",
    "project_code": "TP001",
    "tender_date": "2026-01-01",
    "deadline": "2026-02-01",
    "items": [
        {
            "name": "100x50 槽式桥架",
            "category": "桥架",
            "spec": "100x50",
            "unit": "m",
            "quantity": 100,
            "remark": "热镀锌",
            "extended_attrs": {"surface": "热镀锌", "thickness": "1.5"}
        },
        {
            "name": "DN50 阀门",
            "category": "阀门",
            "spec": "DN50",
            "unit": "套",
            "quantity": 10,
            "remark": "",
            "extended_attrs": {"pressure": "1.6", "body_material": "铸铁", "empty_field": ""}
        },
        {
            "name": "普通材料",
            "category": "",
            "spec": "",
            "unit": "",
            "quantity": None,
            "remark": ""
            # no extended_attrs key at all
        },
        {
            "name": "另一个材料",
            "category": "母线槽",
            "spec": "",
            "unit": "",
            "quantity": None,
            "remark": "",
            "extended_attrs": None  # explicitly None
        }
    ]
}

result = ExtractionPipeline._postprocess_tender(data)
print(json.dumps(result, ensure_ascii=False, indent=2))

# Verify assertions
items = result["items"]
assert len(items) == 4, f"Expected 4 items, got {len(items)}"
assert items[0]["extended_attrs"] == {"surface": "热镀锌", "thickness": "1.5"}
assert items[1]["extended_attrs"] == {"pressure": "1.6", "body_material": "铸铁"}  # empty_field filtered
assert items[2]["extended_attrs"] == {}  # missing key -> empty dict
assert items[3]["extended_attrs"] == {}  # None -> empty dict
print("\nAll assertions passed!")
