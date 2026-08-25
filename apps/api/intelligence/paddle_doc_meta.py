"""paddle_doc_meta.py — 文档级"标量 + N 项声明式要求"的 Paddle 文字层抽取
（docs/design/26 P4 补）。

一份文档除了主清单（招标的采购清单/报价的明细表）之外，通常还有若干文档级
语义要抽取——招标文件要品牌要求、投标单位参与品牌；投标文件要是否明确报出
投标价格、声明总价这类。这些都是同一种形状的"声明式要求"
（`vl_tender.TenderRequirement`：key/title/hint/shape），`vl_tender.py` 的
`extract_tender_requirements` 已经证明了这个模式对招标侧管用——本模块把它的
**调用方式**从"vision 图片 + 提示词"换成"Paddle 已经 OCR 出来的纯文字 +
提示词"，复用同一套声明/解析（`build_requirements_prompt`/`parse_requirements`
/`parse_tender_meta`，纯文本处理，不碰 vision，不需要重写），并且不限定给
招标文件用——调用方传自己的 `TenderRequirement` 集合，投标文件一样能用（比如
`DEFAULT_QUOTE_REQUIREMENTS`）。

## 为什么不用 Paddle 的表格识别做这件事

要求散落在自由文字/半结构化表格里，形状不固定（品牌要求可能是一张表，也
可能是一段话）；vision 路径靠"发一段提示词问模型"天然能处理任意形状，Paddle
的结构化表格识别只在"这一段刚好被 Paddle 切出表格对象"时管用，靠不住当通用
机制（跟 design/26 §2"为什么是工程项目不是配置开关"同一个论证：不能靠"这份
样本恰好长这样"设计架构）。改走文字层是因为 Paddle 已经把整页文字 OCR 出来了
（`page["text"]`/`page["markdown"]`），不需要再额外发一次 vision 调用——省的
是"再拍一次照片问模型"，不是省"用模型理解自由文字"这一步本身。

## 用什么模型做文字抽取

复用 `apps.api.services.llm_provider.get_dashscope_client()`——**不是**重新
引入 qwen 当识别引擎。那个客户端已经在服务 `enhance.py`（AI 后处理分类/
标准化/预对齐）等纯文本、非识别用途，跟 design/26 要删除的"qwen 作为视觉
识别引擎"（`vl_quote.py`/`dashscope_ocr.py` 的 `_mm_stream`/`vl_tender.py`
的 vision 调用）是两回事——这里的输入是 Paddle 已经识别出的纯文字，不是
图片，不占用、不依赖被删除的那条视觉调用链路，也不是重新给识别层挂一个
qwen 兜底（`.claude/rules/recognition.md` 禁的是"视觉识别能力探测后静默降级"，
这里从头到尾没有视觉调用）。
"""
from __future__ import annotations

import logging
from typing import Callable, Sequence

from apps.api.intelligence.vl_tender import (
    _META_KEYS,
    TenderRequirement,
    build_requirements_prompt,
    parse_requirements,
    parse_tender_meta,
)
from apps.api.intelligence.vl_quote import parse_quote_meta

log = logging.getLogger(__name__)

# prompt（已含上下文文字）-> 模型原始文本响应。跟 vl_tender.VLCall 同一个
# "可注入、不内嵌网络调用"的可测试性约定，只是输入从图片换成文字。
TextCall = Callable[[str], str]

PROMPT_META_TEXT = """以下是这份文件前几页经过 OCR 识别出的文字内容（可能有轻微识别噪声）。
请从中提取下面五项，每行一个，格式 key: value：

project_name  项目名称
project_code  项目编号/招标编号
tenderer      招标单位/招标人/采购人全称（发出这份招标文件的单位，不是投标单位）
tender_date   招标日期/发出日期
deadline      投标截止时间

文档上没写的就留空，不要推测。只返回这五行，不要其他说明。

=== 文字内容 ===
"""


def get_text_client_call() -> TextCall | None:
    """生产文字抽取客户端：DashScope 通用文本模型（非视觉，见模块文档）。
    没配 key 时返回 None，调用方按标量/要求缺失处理（跟 vision 路径同一个
    "失败不拖垮主线"约定，不抛异常）。"""
    import os

    from apps.api.core.config import get_settings
    from apps.api.core.domain_config import TEXT_CLIENT_VENDOR
    from apps.api.services.llm_provider import get_dashscope_client

    # design/41：文本类调用可以切到 mimo（订阅制，成本近乎为零）。**默认仍是
    # dashscope**——切换靠 `domain_config.TEXT_CLIENT_VENDOR`，不是靠"哪个 key
    # 配了就用哪个"的隐式探测。没配 mimo 的 key 时**明确回落 dashscope 并记日志**，
    # 不静默失败（`.claude/rules/recognition.md` 禁的是能力探测后的静默降级）。
    if TEXT_CLIENT_VENDOR == "mimo":
        mimo_key = (os.environ.get("MIMO_API_KEY") or "").strip()
        if mimo_key:
            from openai import OpenAI

            from apps.api.core.domain_config import (
                PAGE_FILTER_BASE_URL, PAGE_FILTER_MODEL,
            )
            client = OpenAI(api_key=mimo_key, base_url=PAGE_FILTER_BASE_URL)

            def _mimo_call(prompt: str) -> str:
                resp = client.chat.completions.create(
                    model=PAGE_FILTER_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=60,
                )
                return resp.choices[0].message.content or ""

            return _mimo_call
        log.warning("TEXT_CLIENT_VENDOR=mimo 但没有 MIMO_API_KEY，回落 dashscope")

    client = get_dashscope_client()
    if client is None:
        return None
    settings = get_settings()

    def _call(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=settings.DASHSCOPE_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
        )
        return resp.choices[0].message.content or ""

    return _call


def extract_meta_from_text(page_texts: Sequence[str], text_call: TextCall) -> dict[str, str]:
    """首页文字 → 四个文档级标量。失败不抛异常——标量缺失应当留空并可见，
    不该让整份识别失败（跟 `vl_tender.extract_tender_meta` 同一个约定）。"""
    if not page_texts:
        return {k: "" for k in _META_KEYS}
    try:
        context = "\n\n---\n\n".join(t for t in page_texts if t)
        if not context.strip():
            return {k: "" for k in _META_KEYS}
        return parse_tender_meta(text_call(PROMPT_META_TEXT + context))
    except Exception:                                              # noqa: BLE001
        log.warning("封面元信息抽取失败（Paddle 文字层路径），封面标量留空", exc_info=True)
        return {k: "" for k in _META_KEYS}


# ─── 报价封面元信息（design/27 §7.1）───────────────────────────────────────
#
# qwen 时代 `vl_quote.extract_quote_meta` 靠视觉模型读封面页图像抽
# supplier_name/bid_total/bid_total_basis/tax_rate 四项，喂声明总价核对门
# （quote_confirmation_service._build_checksum）和副本去重（_dedupe_copies）。
# Paddle 切换（design/26 P4）把这条视觉调用整体换成表格识别，四项字段从此
# 没有输入——checksum 门因此一直是 unknown/不阻断，是切换时遗留的静默缺口
# （design/27 §7.1 记录，这里补上）。解析逻辑（parse_quote_meta）跟来源
# 无关，直接复用 vl_quote 的实现，不重写一份。
PROMPT_QUOTE_META_TEXT = """以下是这份投标文件首页/汇总页经过 OCR 识别出的文字内容（可能有轻微识别噪声）。
请从中提取下面四项，每行一个，格式 key: value：

supplier_name      投标单位/报价单位全称
bid_total          投标总价（只要数字，不要货币符号和单位）
bid_total_basis    该总价是否含税：tax_included / tax_excluded / unknown
tax_rate           税率，小数形式（如 13% 写 0.13）

文字内容里没有的就留空，不要推测。只返回这四行，不要其他说明。

=== 文字内容 ===
"""


def extract_quote_meta_from_text(page_texts: Sequence[str], text_call: TextCall) -> dict:
    """封面 1-2 页文字 → 声明总价等四项。失败不抛异常——清单才是主线，不该
    让整份识别因为封面读不出而失败（跟 `vl_quote.extract_quote_meta` 同一个
    约定，只是输入从图片换成 Paddle 已经 OCR 出来的文字）。"""
    empty = parse_quote_meta("")
    if not page_texts:
        return empty
    try:
        context = "\n\n---\n\n".join(t for t in page_texts if t)
        if not context.strip():
            return empty
        return parse_quote_meta(text_call(PROMPT_QUOTE_META_TEXT + context))
    except Exception:                                              # noqa: BLE001
        log.warning("报价封面元信息抽取失败（Paddle 文字层路径），四项留空", exc_info=True)
        return empty


def extract_requirements_from_text(
    page_texts: Sequence[str], text_call: TextCall,
    reqs: Sequence[TenderRequirement],
) -> dict:
    """N 项声明式要求 → 字典。一次调用问全部，不逐项调用——要求散落在同几页
    文字里，逐项问等于把同一段内容重复送 N 遍，且容易让模型混淆语义相近的
    项（跟 vision 路径 `extract_tender_requirements` 同一个理由）。"""
    out = {r.key: ([] if r.shape == "table" else "") for r in reqs}
    if not reqs or not page_texts:
        return out
    try:
        context = "\n\n---\n\n".join(t for t in page_texts if t)
        if not context.strip():
            return out
        prompt = build_requirements_prompt(reqs) + "\n\n=== 文字内容 ===\n" + context
        return parse_requirements(text_call(prompt), reqs)
    except Exception:                                              # noqa: BLE001
        log.warning("文档要求抽取失败（Paddle 文字层路径），留空", exc_info=True)
        return out


# ─── 投标文件默认要求集（design/26 P4 补：投标文件跟招标文件一样支持声明式
#     要求抽取，用户 2026-08-13 明确要求）──────────────────────────────────
# 跟 `vl_tender.DEFAULT_TENDER_REQUIREMENTS` 同一个模式，不同的要求集——
# 加一项 = 加一条声明，不改抽取逻辑、不加新的模型调用点。
DEFAULT_QUOTE_REQUIREMENTS: tuple[TenderRequirement, ...] = (
    TenderRequirement(
        key="price_included", title="是否包含投标价格",
        hint="这份投标文件是否明确报出了投标价格（总价或分项报价）。"
             "只有工程量清单、没有填写单价/总价，或者明确声明另函报价，"
             "都算「未包含」；正文或清单里能看到具体金额才算「已包含」。",
        shape="text"),
)
