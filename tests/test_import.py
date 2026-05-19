"""Tests for Excel/CSV import service."""

import pytest
from apps.api.services.import_service import (
    import_csv_data, _detect_columns, _to_float, _gen_code,
)
from apps.api.models import Material, Quote, Supplier, Project


class TestToFloat:
    def test_normal_number(self):
        assert _to_float("123.45") == 123.45

    def test_comma_decimal(self):
        assert _to_float("123,45") == 123.45

    def test_chinese_comma(self):
        assert _to_float("123，45") == 123.45

    def test_currency_prefix(self):
        assert _to_float("¥123.45") == 123.45

    def test_nan(self):
        import numpy as np
        assert np.isnan(_to_float(None))

    def test_empty_string(self):
        import numpy as np
        assert np.isnan(_to_float(""))

    def test_non_numeric(self):
        import numpy as np
        assert np.isnan(_to_float("abc"))


class TestDetectColumns:
    def test_standard_columns(self):
        cols = ["序号", "名称", "规格型号", "品牌", "单位", "含税单价", "数量", "备注"]
        result = _detect_columns(cols)
        assert result["name"] == "名称"
        assert result["spec"] == "规格型号"
        assert result["brand"] == "品牌"
        assert result["unit"] == "单位"
        assert result["price"] == "含税单价"
        assert result["quantity"] == "数量"
        assert result["remark"] == "备注"

    def test_alternate_column_names(self):
        cols = ["物料名", "规格", "厂家", "计量", "不含税单价", "工程量"]
        result = _detect_columns(cols)
        assert result["name"] == "物料名"
        assert result["brand"] == "厂家"
        assert result["price_excl"] == "不含税单价"
        assert result["quantity"] == "工程量"

    def test_missing_columns(self):
        cols = ["序号", "名称", "备注"]
        result = _detect_columns(cols)
        assert result["name"] == "名称"
        assert result["spec"] is None
        assert result["price"] is None


class TestGenCode:
    def test_first_code_in_category(self, db_session):
        code = _gen_code(db_session, "电气", "桥架")
        assert code == "EL-BRG-00001"

    def test_sequential_code(self, db_session):
        mat = Material(
            material_code="EL-BRG-00005",
            standard_name="test",
            profession="电气",
            category="桥架",
        )
        db_session.add(mat)
        db_session.flush()
        code = _gen_code(db_session, "电气", "桥架")
        assert code == "EL-BRG-00006"


class TestImportCSV:
    def test_import_valid_csv(self, db_session):
        csv_content = (
            "名称,规格型号,品牌,单位,含税单价,数量,备注\n"
            "托盘式桥架,300×150,泰和,m,50.00,100,测试\n"
            "槽式桥架,200×100,振大,m,35.00,200,测试2\n"
        ).encode("utf-8-sig")

        result = import_csv_data(db_session, csv_content, "test.csv", "桥架", "测试项目")

        assert result["status"] == "ok"
        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert result["batch_id"].startswith("IMP-")
        assert len(result["errors"]) == 0

        # Check records created
        mats = db_session.query(Material).filter(Material.category == "桥架").all()
        assert len(mats) == 2
        quotes = db_session.query(Quote).all()
        assert len(quotes) == 2
        assert quotes[0].batch_id == result["batch_id"]

    def test_import_with_empty_rows(self, db_session):
        csv_content = (
            "名称,规格型号,单位,含税单价\n"
            "桥架A,300×150,m,50\n"
            ",,,\n"
            "桥架B,200×100,m,35\n"
        ).encode("utf-8-sig")

        result = import_csv_data(db_session, csv_content, "test.csv", "桥架")
        assert result["imported"] == 2
        assert result["skipped"] == 1  # empty row skipped

    def test_import_unknown_category(self, db_session):
        csv_content = b"name,price\ntest,100\n"
        result = import_csv_data(db_session, csv_content, "test.csv", "未知品类")
        assert result["status"] == "error"
        assert result["imported"] == 0

    def test_import_invalid_file(self, db_session):
        result = import_csv_data(db_session, b"\x00\x01\x02", "test.csv", "桥架")
        assert result["status"] == "error"

    def test_import_creates_supplier(self, db_session):
        # v2: supplier is read from "供应商" column, not brand column
        csv_content = (
            "名称,品牌,供应商,含税单价\n"
            "桥架A,某品牌,测试供应商XYZ,50\n"
        ).encode("utf-8-sig")

        result = import_csv_data(db_session, csv_content, "test.csv", "桥架")
        assert result["imported"] == 1

        sup = db_session.query(Supplier).filter(Supplier.name == "测试供应商XYZ").first()
        assert sup is not None

    def test_import_creates_project(self, db_session):
        csv_content = (
            "名称,含税单价\n"
            "桥架A,50\n"
        ).encode("utf-8-sig")

        result = import_csv_data(db_session, csv_content, "test.csv", "桥架", "新项目")
        assert result["imported"] == 1

        proj = db_session.query(Project).filter(Project.name == "新项目").first()
        assert proj is not None

    def test_import_price_validation(self, db_session):
        csv_content = (
            "名称,含税单价\n"
            "桥架A,-10\n"
            "桥架B,abc\n"
            "桥架C,50\n"
        ).encode("utf-8-sig")

        result = import_csv_data(db_session, csv_content, "test.csv", "桥架")
        assert result["imported"] == 3  # all imported, bad prices stored as None
        quotes = db_session.query(Quote).all()
        # Only the last one should have a valid price
        valid_prices = [q.unit_price for q in quotes if q.unit_price and q.unit_price > 0]
        assert len(valid_prices) == 1
        assert valid_prices[0] == 50.0
