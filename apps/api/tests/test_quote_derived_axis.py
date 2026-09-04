"""design/32 A1+A2：从报价本身派生比价行轴。

用直接构造 `BidSubmission`/`BidQuoteLine` 的方式（不走真实识别/上传），因为
要精确控制的是 `qty=None`（A1 判据）、`document_row_index` 三态、并列条目数
这几个边界值——真实识别链路凑不出这么精确的组合。跟真实链路的对接由
`test_preview_service.py` 里走 HTTP 的集成用例覆盖。
"""
from __future__ import annotations

import pytest

from apps.api.models import Project
from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.services.matrix.quote_derived_axis import (
    NoUsableQuoteRows,
    build_quote_derived_axis,
)


def _make_submission(db, *, sid_hint: str, project_id: int) -> BidSubmission:
    sub = BidSubmission(
        job_id=f"job-{sid_hint}", supplier_raw_name=f"供应商{sid_hint}",
        project_id=project_id, batch_id=f"BID-{sid_hint}", status="confirmed",
    )
    db.add(sub)
    db.flush()
    return sub


def _make_line(db, sub_id: int, *, name: str, qty: float | None, spec: str = "",
               unit: str = "个", category: str = "阀门", document_row_index: int | None = None,
               canonical: dict | None = None) -> BidQuoteLine:
    meta = {"document_row_index": document_row_index} if document_row_index is not None else {}
    line = BidQuoteLine(
        submission_id=sub_id, raw_name=name, standard_name=name, spec=spec,
        unit=unit, qty=qty, category=category, canonical=canonical,
        extraction_meta=meta,
    )
    db.add(line)
    return line


@pytest.fixture
def db(temp_db):
    _engine, SessionLocal = temp_db
    with SessionLocal() as s:
        yield s


@pytest.fixture
def project_id(db):
    p = Project(name="派生轴测试项目", code="QDA-1")
    db.add(p)
    db.flush()
    return p.id


class TestReferenceSelection:
    def test_picks_the_submission_with_most_item_rows(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        b = _make_submission(db, sid_hint="B", project_id=project_id)
        for i in range(3):
            _make_line(db, a.id, name=f"物料{i}", qty=float(i + 1))
        for i in range(5):
            _make_line(db, b.id, name=f"物料{i}", qty=float(i + 1))
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id, b.id])
        assert axis.reference_submission_id == b.id
        assert len(axis.anchors) == 5

    def test_tie_breaks_by_lowest_submission_id(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        b = _make_submission(db, sid_hint="B", project_id=project_id)
        for sub in (a, b):
            for i in range(4):
                _make_line(db, sub.id, name=f"物料{i}", qty=1.0)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [b.id, a.id])  # 顺序倒过来传
        assert axis.reference_submission_id == a.id, "并列时应取 submission_id 更小的那家，不受传参顺序影响"

    def test_candidates_are_recorded_for_diagnosis(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        b = _make_submission(db, sid_hint="B", project_id=project_id)
        for i in range(2):
            _make_line(db, a.id, name="x", qty=1.0)
        for i in range(4):
            _make_line(db, b.id, name="x", qty=1.0)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id, b.id])
        assert dict(axis.candidates) == {a.id: 2, b.id: 4}


class TestA1ItemRowFilter:
    def test_qty_none_rows_are_excluded_from_both_counting_and_anchors(self, db, project_id):
        """A1：qty 是 None 就不是条目行——表头/空行/合计行的典型形状。"""
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="表头行", qty=None)
        _make_line(db, a.id, name="物料1", qty=10.0)
        _make_line(db, a.id, name="合计", qty=None)
        _make_line(db, a.id, name="物料2", qty=20.0)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        assert len(axis.anchors) == 2
        assert [an.name for an in axis.anchors] == ["物料1", "物料2"]

    def test_all_rows_qty_none_raises(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="表头", qty=None)
        db.commit()

        with pytest.raises(NoUsableQuoteRows, match="有效数量"):
            build_quote_derived_axis(db, "阀门", [a.id])

    def test_no_submission_ids_raises(self, db):
        with pytest.raises(NoUsableQuoteRows):
            build_quote_derived_axis(db, "阀门", [])


class TestDocumentOrder:
    def test_uses_document_row_index_when_complete(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        # 故意乱序插入，document_row_index 才是真相
        _make_line(db, a.id, name="第三项", qty=3.0, document_row_index=2)
        _make_line(db, a.id, name="第一项", qty=1.0, document_row_index=0)
        _make_line(db, a.id, name="第二项", qty=2.0, document_row_index=1)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        assert [an.name for an in axis.anchors] == ["第一项", "第二项", "第三项"]
        assert [an.seq for an in axis.anchors] == [1, 2, 3]

    def test_falls_back_to_id_order_when_index_missing_entirely(self, db, project_id):
        """历史数据：全都没有 document_row_index → legacy_order_fallback。"""
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="先插入", qty=1.0)
        _make_line(db, a.id, name="后插入", qty=2.0)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        assert [an.name for an in axis.anchors] == ["先插入", "后插入"]

    def test_falls_back_to_id_order_when_index_partially_present(self, db, project_id):
        """三态判据的第三态：部分行有、部分没有 = 业务序号已损坏，不能信。
        跟 anchor_match._doc_order 的第三态同一个决定——安全回退，不猜。"""
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="有序号", qty=1.0, document_row_index=5)
        _make_line(db, a.id, name="没序号", qty=2.0)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        # 残缺 → 回退入库顺序，不是按那个孤立的 document_row_index=5 排。
        assert [an.name for an in axis.anchors] == ["有序号", "没序号"]


class TestCategoryFilter:
    def test_other_category_rows_are_not_counted_or_included(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="阀门件", qty=1.0, category="阀门")
        _make_line(db, a.id, name="电缆件", qty=1.0, category="电缆")
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        assert len(axis.anchors) == 1
        assert axis.anchors[0].name == "阀门件"


class TestAnchorFields:
    def test_fields_carry_through_and_note_is_explanatory(self, db, project_id):
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="Y型过滤器", qty=12.0, spec="DN20", unit="个",
                  canonical={"valve_type": "过滤器", "dn": "20"})
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        an = axis.anchors[0]
        assert an.name == "Y型过滤器"
        assert an.spec == "DN20"
        assert an.unit == "个"
        assert an.qty == 12.0
        assert an.canonical == {"valve_type": "过滤器", "dn": "20"}
        assert an.source_ref["quote_derived_from_submission_id"] == a.id
        # 说明必须点破这条轴的局限——不能只是一句"已生成"。
        assert "漏报" in axis.note or "不能判断" in axis.note

    def test_canonical_is_computed_when_missing(self, db, project_id):
        """报价行没存 canonical 时（旧数据/非阀门品类不适用）现算一次，
        不留空——否则派生锚点在阀门品类下会比正常招标锚点信息更少。"""
        a = _make_submission(db, sid_hint="A", project_id=project_id)
        _make_line(db, a.id, name="截止阀", qty=1.0, spec="DN25", canonical=None)
        db.commit()

        axis = build_quote_derived_axis(db, "阀门", [a.id])
        assert axis.anchors[0].canonical.get("valve_type") == "截止阀"
