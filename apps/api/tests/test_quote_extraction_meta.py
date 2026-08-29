"""Phase 1: Quote.extraction_meta_json round-trip + raw-evidence shape.

The batch-confirm wiring (raw_material/raw_spec/source_ref/canonical written at
confirm time) is exercised end-to-end by scripts/test_e2e_*.py against the real
backend; here we pin the model-level contract that the JSON column stores and
returns the row-level evidence dict the LLM supplier-fill agent depends on.
"""
from apps.api.models import Material, Supplier, Project, Quote


def test_quote_extraction_meta_json_round_trips(db_session):
    """A Quote must store and return the extraction_meta_json dict verbatim."""
    proj = Project(name="MetaRoundTrip", status="进行中")
    sup = Supplier(name="供应商Meta", short_name="Meta", categories=["阀门"])
    mat = Material(
        material_code="V-META-1", standard_name="球阀 DN50",
        profession="暖通", category="阀门", unit="个",
    )
    db_session.add_all([proj, sup, mat])
    db_session.flush()

    meta = {
        "extraction_job_id": "job-abc123",
        "source_ref": {"sheet": "报价表", "row": 23},
        "raw_material": "不锈钢球阀(原始品名)",
        "raw_spec": "DN50 PN16",
        "raw_unit": "个",
        "raw_remark": "含运费",
        "material_type": "不锈钢",
        "canonical": {"valve_type": "球阀", "dn": "DN50", "pn": "PN16", "material": "不锈钢"},
        "validation_warning": "",
    }
    q = Quote(
        material_id=mat.id, supplier_id=sup.id, project_id=proj.id,
        unit_price=186.0, quantity=10.0, total_price=1860.0,
        extraction_meta_json=meta,
    )
    db_session.add(q)
    db_session.commit()
    qid = q.id

    db_session.expire_all()
    got = db_session.get(Quote, qid)
    assert got.extraction_meta_json == meta
    # The fields the LLM agent reads back must survive
    assert got.extraction_meta_json["raw_material"] == "不锈钢球阀(原始品名)"
    assert got.extraction_meta_json["source_ref"]["row"] == 23
    assert got.extraction_meta_json["canonical"]["material"] == "不锈钢"


def test_quote_extraction_meta_json_nullable(db_session):
    """Legacy rows (no meta) must tolerate NULL."""
    proj = Project(name="MetaNull", status="进行中")
    sup = Supplier(name="供应商Null", short_name="Null", categories=["阀门"])
    mat = Material(
        material_code="V-META-2", standard_name="闸阀 DN80",
        profession="暖通", category="阀门", unit="个",
    )
    db_session.add_all([proj, sup, mat])
    db_session.flush()

    q = Quote(material_id=mat.id, supplier_id=sup.id, project_id=proj.id, unit_price=99.0)
    db_session.add(q)
    db_session.commit()

    got = db_session.get(Quote, q.id)
    assert got.extraction_meta_json is None
