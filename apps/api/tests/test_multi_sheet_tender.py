"""design/24 B1 —— 采购清单 Excel 多 Sheet 支持。

prj2 实测场景：招标 PDF 正文没有采购清单，清单是单独一份 Excel 附件，
且可能有多个 Sheet（如"汇总表"+"明细表"）。此前 parse_tender_xlsx 只读
wb.active，遇到这种附件直接读错 Sheet 或读到空表。
"""
from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from apps.api.services.tender.tender_list import (
    list_tender_sheets,
    parse_tender_xlsx,
    pick_default_sheet,
)


def _xlsx_with_sheets(sheets: dict[str, list[tuple] | None]) -> bytes:
    """构造多 Sheet xlsx。value=None 的 Sheet 只写点无关文本(不像清单)；
    value=list 的 Sheet 写规范表头(序号/名称/规格/单位/数量) + 数据行。
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        if rows is None:
            ws.append(["说明", "本表非采购清单"])
            ws.append(["联系人", "张三"])
        else:
            ws.append(["序号", "名称", "规格", "单位", "数量"])
            for r in rows:
                ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── 纯函数单测 ────────────────────────────────────────────────────────────

def test_list_tender_sheets_marks_list_like_and_others():
    content = _xlsx_with_sheets({
        "封面": None,
        "明细": [(1, "闸阀", "DN50", "个", 1), (2, "球阀", "DN25", "个", 2)],
    })
    sheets = list_tender_sheets(content)
    by_name = {s.name: s for s in sheets}
    assert by_name["封面"].looks_like_list is False
    assert by_name["明细"].looks_like_list is True
    assert by_name["明细"].row_count == 2


def test_pick_default_sheet_prefers_most_data_rows_not_first_match():
    """汇总表排在前面、表头也形似，但数据行少——不该被 auto-detect 选中。"""
    content = _xlsx_with_sheets({
        "汇总表": [(1, "阀门类合计", "—", "项", 1)],           # 表头匹配但只有 1 行
        "明细表": [(1, "闸阀", "DN50", "个", 10), (2, "球阀", "DN25", "个", 20),
                  (3, "止回阀", "DN80", "个", 5)],               # 3 行，更完整
    })
    sheets = list_tender_sheets(content)
    assert pick_default_sheet(sheets) == "明细表"


def test_pick_default_sheet_none_when_nothing_looks_like_list():
    content = _xlsx_with_sheets({"封面": None, "说明": None})
    sheets = list_tender_sheets(content)
    assert pick_default_sheet(sheets) is None


def test_parse_tender_xlsx_explicit_sheet_overrides_active():
    """active sheet（第一个创建的）不是清单，显式指定另一个 Sheet 名照样能解析。"""
    content = _xlsx_with_sheets({
        "封面": None,
        "明细": [(1, "闸阀", "DN50", "个", 1)],
    })
    anchors = parse_tender_xlsx(content, sheet="明细")
    assert len(anchors) == 1
    assert anchors[0].name == "闸阀"
    with pytest.raises(ValueError):
        parse_tender_xlsx(content, sheet="封面")


def test_single_sheet_file_unaffected():
    """单 Sheet 文件：不传 sheet 参数，行为与改动前完全一致。"""
    content = _xlsx_with_sheets({"Sheet1": [(1, "闸阀", "DN50", "个", 1)]})
    anchors = parse_tender_xlsx(content)
    assert len(anchors) == 1
    sheets = list_tender_sheets(content)
    assert len(sheets) == 1
    assert pick_default_sheet(sheets) == "Sheet1"


# ── HTTP 集成：/tender-list/preview ─────────────────────────────────────────

@pytest.fixture
def client(temp_db, monkeypatch, auth_override):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


class TestMultiSheetPreviewEndpoint:
    def test_auto_detect_picks_largest_list_like_sheet(self, client):
        xlsx = _xlsx_with_sheets({
            "汇总表": [(1, "阀门类合计", "—", "项", 1)],
            "明细表": [(1, "闸阀", "DN50", "个", 10), (2, "球阀", "DN25", "个", 20),
                      (3, "止回阀", "DN80", "个", 5)],
        })
        r = client.post(
            "/api/analysis/tender-list/preview",
            files={"file": ("清单.xlsx", xlsx,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["selected_sheet"] == "明细表"
        assert data["total"] == 3
        assert len(data["sheets"]) == 2
        names = {s["name"]: s for s in data["sheets"]}
        assert names["汇总表"]["looks_like_list"] is True
        assert names["汇总表"]["row_count"] == 1
        assert names["明细表"]["row_count"] == 3

    def test_explicit_sheet_param_overrides_auto_detect(self, client):
        xlsx = _xlsx_with_sheets({
            "汇总表": [(1, "阀门类合计", "—", "项", 1)],
            "明细表": [(1, "闸阀", "DN50", "个", 10), (2, "球阀", "DN25", "个", 20)],
        })
        r = client.post(
            "/api/analysis/tender-list/preview",
            data={"sheet": "汇总表"},
            files={"file": ("清单.xlsx", xlsx,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["selected_sheet"] == "汇总表"
        assert data["total"] == 1

    def test_no_list_like_sheet_returns_400(self, client):
        xlsx = _xlsx_with_sheets({"封面": None, "说明": None})
        r = client.post(
            "/api/analysis/tender-list/preview",
            files={"file": ("清单.xlsx", xlsx,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 400, r.text

    def test_single_sheet_response_shape_unchanged(self, client):
        """单 Sheet 文件：sheets 长度为 1，selected_sheet 就是那个 Sheet 名。"""
        xlsx = _xlsx_with_sheets({"Sheet": [(1, "闸阀", "DN50", "个", 1)]})
        r = client.post(
            "/api/analysis/tender-list/preview",
            files={"file": ("清单.xlsx", xlsx,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["sheets"]) == 1
        assert data["selected_sheet"] == "Sheet"
        assert data["total"] == 1
