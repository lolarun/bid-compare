"""test_document_summary.py — design/29 §4。概述只能用给定事实，不能编造
判断——用注入的假 call 验证约束真的生效，不依赖真实模型调用（那部分已经
在手测里跑过真实结果，见提交说明）。"""
from __future__ import annotations

from apps.api.intelligence.document_summary import compose_summary


def test_no_call_falls_back_to_concatenated_facts():
    """design/27 红线1：没有 LLM 也不能让卡片开天窗。"""
    result = compose_summary("tender", {"project_name": "某项目", "row_count": 10}, call=None)
    assert "某项目" in result
    assert "10" in result


def test_empty_facts_says_so_honestly():
    result = compose_summary("bid", {}, call=lambda p: "不应该被调用")
    assert "暂无" in result or "等待" in result


def test_injected_call_receives_only_given_facts_not_raw_document():
    """确认调用方传给 call 的 prompt 里只有结构化事实拼出来的文字，没有
    原始文档内容——这个模块的设计前提就是不碰识别产物，只处理已确认字段。"""
    captured_prompts = []

    def fake_call(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "某项目采购阀门，清单共 10 行。"

    result = compose_summary("tender", {"project_name": "某项目", "category": "阀门", "row_count": 10}, fake_call)
    assert result == "某项目采购阀门，清单共 10 行。"
    assert len(captured_prompts) == 1
    assert "某项目" in captured_prompts[0]
    assert "10" in captured_prompts[0]


def test_call_failure_falls_back_gracefully_not_crash():
    def failing_call(_prompt: str) -> str:
        raise RuntimeError("network down")

    result = compose_summary("bid", {"supplier_name": "某供应商", "row_count": 5}, failing_call)
    assert "某供应商" in result
    assert "5" in result


def test_bid_facts_only_include_bid_relevant_fields():
    """招标事实（deadline）不应该混进投标概述的拼接文本——两边字段集不
    共用，混进去是真的字段用错，不是无害的多余信息。"""
    captured = []
    compose_summary("bid", {"supplier_name": "某供应商", "row_count": 5,
                             "deadline": "不该出现"}, lambda p: captured.append(p) or "ok")
    assert "不该出现" not in captured[0]
