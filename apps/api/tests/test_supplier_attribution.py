"""design/28 §4.2 供应商归属——单文件问题，不是跨文件配对问题。"""
from __future__ import annotations

import pytest

from apps.api.models.supplier import Supplier
from apps.api.services.supplier.attribution import (
    attribute_supplier,
    extract_filename_hint,
)


@pytest.fixture
def seeded_supplier(db_session):
    # short_name 跟"凯硕新正投标清单.xlsx"这类真实文件名切出来的提示精确
    # 相等——resolve_supplier 的精确匹配层需要这个，不是随便设一个近似值。
    sup = Supplier(name="上海凯硕新正机电设备有限公司", short_name="凯硕新正",
                    merge_status="active")
    db_session.add(sup)
    db_session.commit()
    db_session.refresh(sup)
    return sup


def test_extraction_signal_is_primary(db_session, seeded_supplier):
    """封面识别抽到供应商名时，这是唯一有决定权的信号——文件名再怎么冲突
    都不改变 source。"""
    job_result = {"_doc_meta": {"supplier_name": "上海凯硕新正机电设备有限公司"}}
    result = attribute_supplier(db_session, job_result, "跟供应商名完全无关的文件名.pdf")
    assert result.source == "extraction"
    assert result.supplier_name == "上海凯硕新正机电设备有限公司"
    assert result.resolve.supplier is not None
    assert result.resolve.supplier.id == seeded_supplier.id


def test_top_level_supplier_name_used_when_no_doc_meta(db_session, seeded_supplier):
    """_doc_meta 缺失时（比如没配置 text_call），退回顶层 supplier_name——
    这也是"抽取信号"，不是文件名兜底。"""
    job_result = {"supplier_name": "上海凯硕新正机电设备有限公司"}
    result = attribute_supplier(db_session, job_result, "irrelevant.pdf")
    assert result.source == "extraction"


def test_doc_meta_supplier_name_takes_precedence_over_top_level(db_session):
    """_doc_meta 是这次识别真正抽到的值；顶层 supplier_name 可能是别的环节
    覆盖过的——两者都在时以 _doc_meta 为准（跟 tier1_signals 的同名判断
    保持一致的优先级）。"""
    job_result = {
        "supplier_name": "别的名字",
        "_doc_meta": {"supplier_name": "识别抽到的名字"},
    }
    result = attribute_supplier(db_session, job_result, "irrelevant.pdf")
    assert result.supplier_name == "识别抽到的名字"


def test_filename_hint_only_when_extraction_missing(db_session, seeded_supplier):
    """抽取信号缺失（招标侧产物、或识别没抽到）时才退到文件名——这是唯一
    合法触发文件名启发式的路径，§5 red line 2 的具体落地。"""
    job_result = {"items": []}  # 没有 supplier_name / _doc_meta
    result = attribute_supplier(db_session, job_result, "凯硕新正投标清单.xlsx")
    assert result.source == "filename_hint"
    assert result.supplier_name == "凯硕新正"
    assert result.resolve.supplier is not None
    assert result.resolve.supplier.id == seeded_supplier.id


def test_extraction_blank_string_falls_through_to_filename(db_session):
    """_doc_meta.supplier_name 存在这个 key 但值是空白——等同于没抽到，
    照样退到文件名，不能因为 key 存在就误判成"抽取命中了空结果"。"""
    job_result = {"_doc_meta": {"supplier_name": "   "}}
    result = attribute_supplier(db_session, job_result, "凯硕新正投标清单.xlsx")
    assert result.source == "filename_hint"


def test_unresolved_when_no_signal_at_all(db_session):
    """纯数字文件名（扫描批次号一类）不构成有效的文件名提示——两路证据
    都没有，必须诚实地报"没有信号"，不能硬凑一个看似有意义的候选。"""
    job_result = {"items": []}
    result = attribute_supplier(db_session, job_result, "12345.pdf")
    assert result.source == "unresolved"
    assert result.supplier_name is None
    assert result.resolve is None


def test_filename_hint_recorded_even_when_extraction_wins(db_session, seeded_supplier):
    """审计用途：即便 source="extraction"，filename_hint 字段依然要填——
    方便事后复核"识别结果跟文件名是否吻合"，但这条记录从不参与决策本身
    （决策已经在 extraction 分支就做完了，哪怕文件名提示指向完全不同的名字）。"""
    job_result = {"_doc_meta": {"supplier_name": "上海凯硕新正机电设备有限公司"}}
    result = attribute_supplier(db_session, job_result, "泰科龙投标文件.pdf")
    assert result.source == "extraction"
    assert result.supplier_name == "上海凯硕新正机电设备有限公司"
    assert result.filename_hint == "泰科龙"


def test_extract_filename_hint_strips_extension_and_bid_vocabulary():
    assert extract_filename_hint("凯硕新正投标清单.xlsx") == "凯硕新正"
    assert extract_filename_hint("上海绵存投标文件.pdf") == "上海绵存"


def test_extract_filename_hint_empty_when_nothing_left():
    assert extract_filename_hint("投标文件.pdf") == ""


def test_extract_filename_hint_rejects_pure_numeric():
    """扫描批次号、系统生成的临时文件名一类——纯数字残片不是供应商名候选。"""
    assert extract_filename_hint("12345.pdf") == ""
    assert extract_filename_hint("20260814.pdf") == ""


def test_resolve_ambiguous_candidates_surface_not_autoselect(db_session):
    """两家不同供应商共用同一个简称——resolve_supplier 在 short_name 精确
    匹配层撞到两个，走到候选层；attribution 原样透传，不擅自替用户选一个
    （resolve_supplier 不做子串模糊匹配，只在某一层精确匹配撞到多个时才
    产生 candidates——这条测试专门锁住"多个精确匹配"这个真实的歧义来源，
    不是"名字里含某个子串"那种更宽泛、resolve_supplier 根本不支持的匹配）。"""
    db_session.add_all([
        Supplier(name="上海凯硕新正机电设备有限公司", short_name="凯硕新正", merge_status="active"),
        Supplier(name="上海凯硕新正智能科技有限公司", short_name="凯硕新正", merge_status="active"),
    ])
    db_session.commit()
    job_result = {"_doc_meta": {"supplier_name": "凯硕新正"}}
    result = attribute_supplier(db_session, job_result, "irrelevant.pdf")
    assert result.resolve.supplier is None
    assert result.resolve.candidates is not None
    assert len(result.resolve.candidates) == 2


def test_resolve_no_match_at_all_returns_none_not_candidates(db_session):
    """resolve_supplier 不做子串/模糊匹配——查询词是已有供应商全名的子串，
    但没有任何一层精确命中时，必须干脆地返回"没找到"（supplier=None,
    candidates=None），不能误报成"有候选"。"""
    db_session.add(Supplier(name="上海凯硕新正机电设备有限公司", merge_status="active"))
    db_session.commit()
    job_result = {"_doc_meta": {"supplier_name": "凯硕新正"}}  # 全名的子串，非精确匹配
    result = attribute_supplier(db_session, job_result, "irrelevant.pdf")
    assert result.resolve.supplier is None
    assert result.resolve.candidates is None
