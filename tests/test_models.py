"""Tests for database models."""

from apps.api.models import Material, Supplier, Project, Quote, PROFESSION_MAP, CATEGORY_ABBR


def test_material_creation(db_session):
    mat = Material(
        material_code="WS-VLV-00001",
        standard_name="蝶阀",
        profession="给排水",
        category="阀门",
        sub_category="蝶阀",
        spec="DN100",
        unit="个",
    )
    db_session.add(mat)
    db_session.commit()
    db_session.refresh(mat)

    assert mat.id is not None
    assert mat.material_code == "WS-VLV-00001"
    assert mat.category == "阀门"
    assert mat.extended_attrs == {}


def test_material_extended_attrs(db_session):
    mat = Material(
        material_code="WS-VLV-00002",
        standard_name="闸阀",
        profession="给排水",
        category="阀门",
        sub_category="闸阀",
        extended_attrs={
            "nominal_diameter": "DN100",
            "nominal_pressure": "PN16",
            "connection_type": "法兰",
        },
    )
    db_session.add(mat)
    db_session.commit()
    db_session.refresh(mat)

    assert mat.extended_attrs["nominal_diameter"] == "DN100"
    assert mat.extended_attrs["nominal_pressure"] == "PN16"


def test_supplier_creation(db_session):
    sup = Supplier(
        name="上海某桥架厂",
        categories=["桥架", "母线槽"],
        win_count=5,
    )
    db_session.add(sup)
    db_session.commit()
    db_session.refresh(sup)

    assert sup.id is not None
    assert "桥架" in sup.categories


def test_quote_relationships(sample_material, sample_supplier, sample_project, db_session):
    q = Quote(
        material_id=sample_material.id,
        supplier_id=sample_supplier.id,
        project_id=sample_project.id,
        unit_price=48.0,
        quantity=200.0,
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)

    assert q.material.standard_name == "托盘式热浸镀锌桥架"
    assert q.supplier.name == "测试供应商A"
    assert q.project.name == "测试项目一期"


def test_profession_map():
    assert PROFESSION_MAP["桥架"] == "电气"
    assert PROFESSION_MAP["阀门"] == "给排水"
    assert PROFESSION_MAP["风口风阀"] == "暖通"
    assert len(PROFESSION_MAP) == 10


def test_category_abbr():
    assert CATEGORY_ABBR["桥架"] == "BRG"
    assert CATEGORY_ABBR["阀门"] == "VLV"
    assert len(CATEGORY_ABBR) == 10
