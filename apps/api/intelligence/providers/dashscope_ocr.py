"""DashScopeOCRProvider — two-stage OCR+LLM extraction via Alibaba DashScope.

Stage 1: Qwen-VL-OCR (table_parsing) → HTML tables per page
Stage 2: qwen3.6-flash → structured JSON per page
Stage 2b (fallback): if supplier_name still empty after aggregation, OCR the cover
         page(s) and ask LLM specifically for the company name.

Config (apps/api/.env):
  DASHSCOPE_API_KEY    — single key (used when DASHSCOPE_API_KEYS is absent)
  DASHSCOPE_API_KEYS   — comma-separated key list for multi-key rotation
                          (e.g. sk-aaa,sk-bbb — up to N×6 concurrent pages)
  DASHSCOPE_BASE_URL   — default https://dashscope.aliyuncs.com/compatible-mode/v1
  DASHSCOPE_OCR_MODEL  — default qwen-vl-ocr-latest
  DASHSCOPE_LLM_MODEL  — default qwen3.6-flash
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import dashscope
import openai
from openai import OpenAI

from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError,
)

log = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRY_BASE = 2          # exponential backoff base: 2, 4, 8, 16, 32 (with jitter)
_RETRY_MAX = 30          # cap at 30s


class _StreamedResponse:
    """把流式分片拼好后伪装成一次性响应，让 _mm_call 的重试逻辑不必分叉。"""

    __slots__ = ("text",)
    status_code = 200
    message = ""

    def __init__(self, text: str) -> None:
        self.text = text


def _retry_wait(attempt: int) -> float:
    """Exponential backoff with full jitter: min(base * 2^attempt, max) + uniform(0, 1)."""
    return min(_RETRY_BASE * (2 ** attempt), _RETRY_MAX) + random.uniform(0, 1)
# 每个 API key 同时打到 Qwen-VL-OCR 的最大并发调用数（真正的"OCR 并发识别数"）。
# 这是所有 OCR 调用（批量识别/切片/方向探测）的硬上限。env 可调；注意 Qwen 账号本身
# 有 QPS/并发限额，调太高会触发 429（有退避重试，但会拖慢）。多 key 时总并发 = 本值 × key数。
_PER_KEY_CONCURRENCY = max(1, int(os.getenv("OCR_PER_KEY_CONCURRENCY", "6")))

# 信号量登记表放在**模块作用域**、按 api_key 共享，不是实例属性——provider
# 有多个构造点：main.py 启动时建一份长驻的（识别任务走 ThreadPoolExecutor
# 共用它），而 scanned_pdf_classify.get_scanned_classify_call() 是**每次
# HTTP 请求新建一个**。实例私有的信号量意味着这两类调用各持一份配额、彼此
# 不排队，`_PER_KEY_CONCURRENCY` 对同一个 key 的真实并发就不再是硬上限
# （每请求一份 = 限流形同虚设）。按 key 共享后，同一 key 无论由谁发起，
# 进程内总并发都受这一个闸门约束。多 key 时总并发仍是 本值 × key数。
_KEY_SEMAPHORES: dict[str, threading.Semaphore] = {}
_KEY_SEMAPHORES_LOCK = threading.Lock()


def _shared_key_semaphore(key: str) -> threading.Semaphore:
    """取（必要时创建）某个 api_key 的进程级并发闸门。"""
    with _KEY_SEMAPHORES_LOCK:
        sem = _KEY_SEMAPHORES.get(key)
        if sem is None:
            sem = _KEY_SEMAPHORES[key] = threading.Semaphore(_PER_KEY_CONCURRENCY)
        return sem
# ── Stage 2 prompts (OCR HTML → structured JSON) ────────────────────────
_TENDER_S2_PROMPT = """你是机电材料招投标助理。下面是OCR识别出的HTML表格内容。
请从中提取采购材料清单，返回严格的JSON格式。

要求：
- 只提取材料/设备条目，不要表头、合计行、小计行
- 材料名称按原文，不要简化
- 品类从以下选项选择：桥架、母线槽、配电箱、电缆、阀门、不锈钢管、水箱、潜水泵、风口风阀、风机盘管、空调泵；无法判断留空
- 数量若为'若干'等非数字，留null
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "投标单位名称", "items": [{"name": "材料名称", "category": "品类", "spec": "规格型号", "unit": "单位", "quantity": 数量或null, "remark": "备注"}]}

如果该页没有材料清单（如封面、证书等非清单页），返回 {"items": []}"""

_QUOTE_S2_PROMPT = """你是机电材料报价单解析助理。下面是OCR识别出的HTML表格内容。
请从中提取报价明细，返回严格的JSON格式。

要求：
- 【完整性】逐行提取，不要遗漏任何一条材料报价行；表格有多少数据行就返回多少条
- 只提取材料报价行，不要表头、合计行、小计行
- 总价若已标注使用原值，否则留null（不要自己计算）
- 税率用小数如0.13表示13%；品牌按原文；无法识别的字段返回空字符串或null

【价格字段——按表头文字严格映射，不按列顺序推断】

  unit_price_excl_tax（不含税单价）：表头含"不含税"/"税前"时填此字段
  unit_price_incl_tax（含税单价）：表头含"含税"/"综合单价"/"价税"时填此字段
  unit_price（单价，仅表头无含税/不含税标注时才填）：若表头已区分，则留null
  total_price_excl_tax（不含税合计）：表头含"不含税合计"/"金额(不含税)"/"合计(不含税)"时填
  total_price_incl_tax（含税合计）：表头含"价税合计"/"含税合计"/"合价(含税)"/"合计(含税)"时填
  total_price（合计，仅表头无含税/不含税标注时才填）：若表头已区分，则留null
  tax_amount（税额）：表头含"税额"/"增值税额"时填；无此列留null
  model（型号）：表头独立"型号"列时填；规格型号合并在一列时归入spec

【含税/不含税同时存在的典型处理】
表头为"单价(不含税)"/"合计(不含税)"/"税额"/"税率"/"单价(含税)"/"价税合计"时：
- unit_price_excl_tax = "单价(不含税)"列值
- total_price_excl_tax = "合计(不含税)"列值
- tax_amount = "税额"列值
- unit_price_incl_tax = "单价(含税)"列值（若有该列）
- total_price_incl_tax = "价税合计"列值
- unit_price、total_price 留null

【续表页（无表头行）的含税/不含税识别】
当前页若无列头行（即续表页），须通过数值关系识别含税与不含税列：
- 不含税合价 × (1 + 税率) ≈ 含税合价（如税率=13%：不含税 × 1.13 ≈ 含税）
- 税额 ≈ 不含税合价 × 税率
- 不含税单价 × 数量 ≈ 不含税合价；含税单价 × 数量 ≈ 含税合价
- 因此：较小的合价列 = 不含税合价 → total_price_excl_tax；较大的（约=前者×1.13）= 含税合价 → total_price_incl_tax
- 同理，若存在两列单价，较小的单价列 = 不含税单价 → unit_price_excl_tax；较大的（≈较小单价×1.13）= 含税单价 → unit_price_incl_tax
- 若含税单价列中某行值为负数（如-791.00），是OCR识别误差，应取其绝对值（791.00）作为含税单价
- 严禁将"税额"列或"含税合价×1.13"计算值填入 total_price_incl_tax；含税合价必须直接读自对应列原值
- 若页面有"税额"列，验证：税额 ÷ 不含税合价 ≈ 税率（误差<2%），否则说明列识别有误需重新判断

【转置式报价表处理】
当HTML表格格式为"每列对应一个产品、每行对应一个属性"时（最后一行全为连续整数序号，如 50/51/52），
请按列提取，每列=一条报价明细。各行含义（从上到下）：
- 行0: 系统分类（给水系统/排水系统/给排水）→ remark
- 行1: 品牌（如"伯尔梅特"）→ brand
- 行2: 含税合价（最上面的大数字行）→ total_price_incl_tax；unit_price_incl_tax = 含税合价÷数量
- 行3: 税额 → tax_amount
- 行4: 税率（13%）→ tax_rate=0.13
- 行5: 不含税合价 → total_price_excl_tax
- 行6: 不含税单价 → unit_price_excl_tax
- 行7: 数量（整数）或单位（"个"）混排——整数为数量，"个"/"套"为单位
- 行8: 单位（如单独一行）
- 中间若干行: 材质（不锈钢/球墨铸铁等）
- 倒数第4行: 规格（DN尺寸）→ spec
- 倒数第3行: 品名（闸阀/蝶阀等阀门名称）→ material
- 倒数第2行: 专业（给排水/给水系统等）→ remark（不要作为品名）
- 最后一行: 序号整数（如1/2/3…89）→ 填入该列的 seq 字段（不要把该行作为独立条目）
重要：若某列的"品名行"（倒数第3行）填写的是系统类别词（给排水/给水系统/排水系统），
该列为小计行，不要提取为报价明细。

对于阀门类材料（截止阀/闸阀/止回阀/球阀/蝶阀/减压阀/疏水阀/过滤器等），
额外填写 canonical 对象：valve_type/dn/pn/material/connection；非阀门类留空对象 {}。

OCR 纠错（阀门类）：normalized_material（确信时填），ocr_correction_reason（纠错依据）。
合法词表：闸阀/截止阀/止回阀/球阀/蝶阀/橡胶瓣止回阀/节能消声止回阀/缓闭式止回阀/低阻力倒流防止器/倒流防止器/小阻力可调式减压阀组/减压阀组/Y型过滤器
material 字段仍按原文填写；normalized_material 仅在确认为OCR错别字时才填，不确定留空。

返回JSON格式：
{"supplier_name": "供应商名称或空字符串", "items": [{"seq": "序号或空字符串", "material": "材料名称", "spec": "规格型号", "model": "型号或空字符串", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": null或单价（仅表头无含税/不含税标注时才填，否则留null）, "unit_price_excl_tax": null或不含税单价, "unit_price_incl_tax": null或含税单价, "total_price": null或合计（仅表头无税种标注时才填，否则留null）, "total_price_excl_tax": null或不含税合计, "total_price_incl_tax": null或含税合计, "tax_rate": 税率小数, "tax_amount": null或税额, "material_type": "材质", "remark": "备注", "canonical": {}, "normalized_material": "", "ocr_correction_reason": ""}]}

转置式报价表：seq = 该列最后一行的整数，非转置表时 seq = 该行的序号列值。
如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""

_META_S2_PROMPT = """你是机电材料招投标助理。下面是投标文件封面/汇总页或营业执照/资质证书页的OCR HTML内容。
请提取元信息，只返回JSON：
{"supplier_name": "投标单位全称或空字符串", "bid_total": 投标总价数字或null, "bid_total_basis": "tax_included|tax_excluded|unknown", "tax_rate": 税率小数或null}
提取规则：
- supplier_name：优先从"投标单位"/"报价单位"/"投标人"字段取；若为营业执照页，从"名称"/"称"字段取公司全名（即使列标题因OCR截断只剩"称"）；不要填经销商授权书中的品牌商名称。
- 若该页没有相关信息，对应字段返回null或空字符串。"""

# Max cover/summary pages to process for supplier name / bid total extraction
MAX_META_PAGES = 5

# ── 视觉页面分类（qwen3-vl-flash / plus）────────────────────────────────────
_VISUAL_FLASH_MODEL = "qwen3-vl-flash"
_VISUAL_PLUS_MODEL = "qwen3-vl-plus"
_VISUAL_PROMPT_VERSION = "v4"   # bump when prompt/roles change → cache invalidates
_VISUAL_THUMB_MAX_PX: int = 2_000_000   # thumbnail pixel budget for Flash batch calls
_VISUAL_TEMPERATURE: float = 0.0        # must be 0 for deterministic cache-safe results

# design/29 §3 Tier 1.5：扫描件招标/投标粗判定。2026-08-21 从"只送第一页
# 缩略图"改成"送前几页原生分辨率图"，页数/分辨率由调用方（scanned_pdf_classify.py）
# 决定送几张，这里只定分辨率预算。跟页面角色分类判据方向相反——那边刻意不看
# 供应商名/项目名，这里恰恰要看，示例用虚构占位符（.claude/rules/recognition.md：
# "生产 prompt 禁止出现真实供应商、项目、文件名"）。
_CLASSIFY_DOC_KIND_MAX_PX: int = 6_000_000   # 高于页面角色分类的缩略图预算——
                                              # 缩略图版实测 0/7，分辨率不够看清是原因之一
_CLASSIFY_DOC_KIND_PROMPT = """这是一份机电材料招投标文档的前几页图片（按顺序，可能含封面、投标函、
授权书等）。请判断这份文档整体是"招标文件"（采购方发布）还是"投标文件"
（供应商提交的报价文件）。

**重要提醒（实测发现的常见易错点）**：投标文件的封面经常沿用招标方发的
封面模板，模板上会同时印着"招标单位"和"投标单位"两个字段——这不代表
文档是招标文件。判断关键看：
- "投标单位"/"投标人"字段是否填了一个具体的公司名（填了→这是投标文件）
- 文档标题/水印是否明确写"投标文件"/"投标函"/"报价文件"
- 后续页面（投标函、法定代表人授权书、报价说明）只会出现在投标文件里，
  招标文件不会有这些内容
只有"招标单位"字段被填、"投标单位"字段空白或未出现具体公司名时，才判
tender。

只看给出的这几页能看到的信息判断，不要假设、不要根据文件名猜测（你也看
不到文件名）。信息真的不够判断时诚实答"uncertain"，不要为了给出答案而猜。

举例（虚构，仅示意格式，不代表任何真实项目）：
- 封面模板印"招标单位：____ 投标单位：某某机电设备有限公司"，标题写
  "投标文件" → doc_type=bid（不要因为看到"招标单位"字样就判成 tender）
- 封面写"XX工程招标文件""招标编号：ZB-2026-001"，没有具体投标单位名 → doc_type=tender
- 信息不足以判断 → doc_type=uncertain

严格只返回 JSON（不要解释、不要 markdown 围栏）：
{"doc_type": "tender"或"bid"或"uncertain", "project_name_hint": "看到的项目名，没有则空字符串", "supplier_name_hint": "看到的投标单位/供应商名，没有则空字符串", "evidence": ["简短依据1", "依据2"]}"""

_VISUAL_VALID_ROLES = (
    "cover bid_letter tender_table_header tender_table_continuation "
    "quote_table_header quote_table_continuation subtotal_or_summary "
    "brand_requirement technical_spec component_parameter_table certificate other"
)

_VISUAL_ROLE_DEFS = """角色定义（role 只能取下列12个值之一，其他任何字符串均无效）：
- cover：封面/标题页（项目名、投标单位名，无明细表）
- bid_letter：投标函/授权委托书/承诺函等信函正文
- tender_table_header：【招标/采购清单】表头页，含序号/名称/规格/单位/数量等列名（招标文件专用）
- tender_table_continuation：招标清单续页，无表头但列结构延续上一页清单（招标文件专用）
- quote_table_header：【投标/报价清单】表头页，含单价/合价/价税等价格列名（投标文件专用）
- quote_table_continuation：报价清单续页，无表头但列结构延续上一页报价表（投标文件专用）
- subtotal_or_summary：汇总页（只有总额/小计行，无逐条物料明细；has_line_items 必须为 false）
- brand_requirement：招标情况表/品牌要求登记表（各供应商参与品牌列表）
- technical_spec：技术规范/施工说明/技术条款/安装要求等正文
- component_parameter_table：部件材质参数表（无数量无报价，仅尺寸/材质/压力等级等）
- certificate：营业执照/资质证书/荣誉证书/检测报告等
- other：目录/TOC/公司介绍/照片/签名页等，以上都不是的

文档类型专属判定规则（必须遵守）：
- 若当前文档是【招标文件】（采购方发布）：含逐条采购明细的表格一律用 tender_table_header /
  tender_table_continuation，绝不使用 quote_table_*（即使表中含单价/合价列也如此）。
- 若当前文档是【投标文件】（供应商发布）：含逐条报价明细的表格一律用 quote_table_header /
  quote_table_continuation，绝不使用 tender_table_*。

通用判定要点：
- header 与 continuation：本页有列名行 → header；直接以数据行开头（无列名）→ continuation，
  continues_from_page 填上一页页码。
- subtotal_or_summary 的关键判定：只有当 has_line_items=false（无逐条明细行）才能判此角色；
  即使有合计行，只要同时有逐条明细行，也应判 *_table_continuation（has_line_items=true）。
- technical_spec / component_parameter_table 常含"规格/材质/阀体"等词，但没有逐条数量+单价+合价，
  不可判为任何 *_table_header/continuation。
- 一页同时有正文段落和明细表 → mixed_content=true，role 取占主体的类型。
- orientation：页面相对正立的旋转角度，只能是 0/90/180/270（扫描件侧向时常见90/270）。"""


def _build_visual_prompt_parts(doc_type: str, page_numbers: list[int]) -> tuple[str, str]:
    """Return (intro, tail) for interleaved PAGE-N content construction.

    Callers should build content as:
      [text(intro), text("PAGE N"), image, text("PAGE M"), image, ..., text(tail)]
    This explicitly binds each image to its page number, preventing mis-attribution
    in multi-image batches.
    """
    if doc_type == "tender":
        kind = "招标文件（采购方发布的招标采购文件，含采购清单/招标情况表）"
        type_note = "本文档是招标文件，所有含逐条采购明细的表格必须用 tender_table_* 角色。"
    else:
        kind = "供应商投标文件（供应商投标报价文件，含报价清单）"
        type_note = "本文档是投标文件，所有含逐条报价明细的表格必须用 quote_table_* 角色。"
    intro = (
        f"你是机电材料招投标文档的页面分类助手。当前文档类型：{kind}。\n"
        f"{type_note}\n"
        f"以下每张图片前均标注了对应的页码（PAGE N），请逐页根据视觉版面判断角色。"
    )
    tail = f"""
{_VISUAL_ROLE_DEFS}

每页必须同时输出以下语义字段（用于代码级确定性校验，不得省略）：
- has_line_items：true=本页有逐条明细行（品名/规格/数量/单价/合价等多列，每物料一行）；
  false=只有汇总/合计，无逐条明细；null=无法从图像确定。
  【重要】has_line_items=true 时 role 不可为 subtotal_or_summary，即使同时有合计行。
- estimated_line_item_count：估计明细行数（整数，0=无，null=不确定）。
- has_column_header：true/false/null — 本页是否有列名行（品名/规格/数量/单价等表头行）。
- has_total_row：true/false/null — 本页是否有合计/小计/总价行。
- table_structure_continues：true/false/null — 本页表格列结构是否与前一页一致（续表标志）。

严格只返回 JSON（不要解释、不要 markdown 围栏）：
{{"pages": [{{"page": {page_numbers[0]}, "role": "...", "confidence": 0.0到1.0, "contains_table": true或false, "orientation": 0, "continues_from_page": null或页码, "mixed_content": false, "has_line_items": null, "estimated_line_item_count": null, "has_column_header": null, "has_total_row": null, "table_structure_continues": null, "evidence": ["简短依据1","依据2"]}}]}}

要求：
- 必须为给出的每一页各返回一条，page 用真实页码（不得遗漏任何页面）。
- role 只能是上述12个值之一；其他任何字符串（含"quote_table"等缩写）均无效。
- 不得依赖供应商名称、固定页码、具体物料名做判断，只看版面结构与列语义。
- 不确定时给低 confidence（≤0.7），并在 evidence 写明疑点。"""
    return intro, tail


def _build_visual_prompt(doc_type: str, page_numbers: list[int]) -> str:
    """Legacy single-string form — kept for Plus review calls where no images are interleaved."""
    intro, tail = _build_visual_prompt_parts(doc_type, page_numbers)
    pages_str = "、".join(str(p) for p in page_numbers)
    return (f"{intro}\n以下是第 {pages_str} 页的图片（共 {len(page_numbers)} 张，顺序与页码一一对应）：\n"
            + tail)


_VISUAL_REVIEW_SUFFIX = """以上为需要复核的页面高清图（第一张）及其前后相邻页缩略图（用于判断续表关系）。
Flash 初判结果与理由如下，请你看高清原图重新裁决（尤其：
- header/continuation 混淆（看本页有无列名行）
- 技术规范/证书被误判为报价表
- 招标文件的采购清单被误判为 quote_table（应为 tender_table）
- orientation 旋转方向
- 含合计行的续表被误判为 subtotal_or_summary）：
{flash_json}
{chain_context_note}

复判必须明确回答 has_line_items（此字段是代码级确定性校验的输入，不得为 null 除非真的无法判断）：
- true：本页有逐条明细行（品名/规格/数量/单价/合价等多列，每物料占一行）
  → role 必须为 *_continuation（即使同时有合计行，也不得判 subtotal_or_summary）
- false：本页只有汇总/合计/总价，无逐条物料明细
  → role 才可判 subtotal_or_summary
- null：图像确实无法判断（须在 evidence 写明原因）

有效 role 值（共12个）：cover bid_letter tender_table_header tender_table_continuation
quote_table_header quote_table_continuation subtotal_or_summary brand_requirement
technical_spec component_parameter_table certificate other

严格只返回单页 JSON（同协议，不要解释，不要 markdown）：
{{"page": {page}, "role": "...", "confidence": 0.0到1.0, "contains_table": true或false, "orientation": 0, "continues_from_page": null或页码, "mixed_content": false, "has_line_items": null, "estimated_line_item_count": null, "has_column_header": null, "has_total_row": null, "table_structure_continues": null, "evidence": ["依据"]}}"""


class DashScopeOCRProvider(LLMProvider):
    """Two-stage provider: OCR (table_parsing) → text LLM (JSON extraction).

    Supports multi-key rotation via DASHSCOPE_API_KEYS env var.  Each key gets
    its own semaphore (max _PER_KEY_CONCURRENCY concurrent calls) so that a
    single slow key doesn't block the others.  429 responses and transient
    connection errors are retried with linear backoff up to _MAX_RETRIES times.
    """

    name = "dashscope_ocr"

    DEFAULT_OCR_MODEL = "qwen-vl-ocr-latest"
    DEFAULT_LLM_MODEL = "qwen3.6-flash"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        ocr_model: str | None = None,
        llm_model: str | None = None,
    ):
        # Resolve key list: DASHSCOPE_API_KEYS (comma-separated) > api_key arg > single env key
        keys_env = os.getenv("DASHSCOPE_API_KEYS", "")
        keys = [k.strip() for k in keys_env.split(",") if k.strip()]
        if not keys:
            single = api_key or os.getenv("DASHSCOPE_API_KEY", "")
            if not single:
                raise ProviderError(
                    "DASHSCOPE_API_KEY not set; cannot initialise DashScopeOCRProvider"
                )
            keys = [single]

        self._keys = keys
        self._key_cycle = itertools.cycle(keys)
        self._key_lock = threading.Lock()
        # 进程级共享（见 _shared_key_semaphore）——不要改回 per-instance。
        self._per_key_sem: dict[str, threading.Semaphore] = {
            k: _shared_key_semaphore(k) for k in keys
        }
        self._llm_clients: dict[str, OpenAI] = {}
        self._client_lock = threading.Lock()

        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_BASE_URL)
        self.ocr_model = ocr_model or os.getenv("DASHSCOPE_OCR_MODEL", self.DEFAULT_OCR_MODEL)
        self.llm_model = llm_model or os.getenv("DASHSCOPE_LLM_MODEL", self.DEFAULT_LLM_MODEL)
        self.model = f"{self.ocr_model}+{self.llm_model}"

        log.info(
            "DashScopeOCRProvider ready — %d key(s), concurrency=%d/key, ocr=%s, llm=%s",
            len(keys), _PER_KEY_CONCURRENCY, self.ocr_model, self.llm_model,
        )

    # ─── key / client helpers ─────────────────────────────────────────────

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def _get_client(self, key: str) -> OpenAI:
        with self._client_lock:
            if key not in self._llm_clients:
                self._llm_clients[key] = OpenAI(api_key=key, base_url=self.base_url)
            return self._llm_clients[key]

    # ─── public API (called per-page by pipeline) ─────────────────────────

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
        page_html: str | None = None,
        table_grids=None,  # unused — kept for LLMProvider signature compatibility
    ) -> ExtractionResponse:
        """Two-stage extraction for a single page image (abstract LLMProvider.extract).

        No production caller remains — VL-direct (vl_extract_csv) replaced the
        per-page OCR→HTML→LLM chain this served (best-practice review F1/F2,
        2026-08-11). Kept because `LLMProvider.extract` is `@abstractmethod`
        (base.py) — removing this implementation would make the class
        uninstantiable. The table_grids structured-input path
        (table_parser.grids_to_llm_json) was deleted with the legacy chain
        that was its only producer.

        If page_html is provided, Stage 1 OCR is skipped and the cached HTML
        is used directly.
        """
        if not images:
            raise ProviderError("extract() requires at least one image")

        t0 = time.time()
        total_tokens = 0

        if page_html is not None:
            html = page_html
            ocr_tokens = 0
        else:
            html, ocr_tokens = self._ocr_page(images[0])
            total_tokens += ocr_tokens

        if not html.strip():
            return ExtractionResponse(
                data={"items": []},
                raw_text="",
                tokens_used=total_tokens,
                provider=self.name,
                duration_ms=int((time.time() - t0) * 1000),
            )

        doc_type = self._guess_doc_type(prompt)
        data, raw_text, llm_tokens = self._llm_parse(html, doc_type)
        total_tokens += llm_tokens

        return ExtractionResponse(
            data=data,
            raw_text=raw_text,
            tokens_used=total_tokens,
            provider=f"{self.name}:{self.model}",
            duration_ms=int((time.time() - t0) * 1000),
        )

    def extract_doc_meta(self, meta_htmls: list[str]) -> dict:
        """Extract document-level metadata from cover/summary page HTMLs.

        Returns: {supplier_name, bid_total, bid_total_basis, tax_rate}
        """
        if not meta_htmls:
            return {
                "supplier_name": None,
                "bid_total": None,
                "bid_total_basis": "unknown",
                "tax_rate": None,
            }

        combined_html = "\n---\n".join(meta_htmls[:MAX_META_PAGES])
        t0 = time.time()
        key = self._next_key()
        client = self._get_client(key)
        sem = self._per_key_sem[key]
        sem.acquire()
        try:
            resp = client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": _META_S2_PROMPT},
                    {"role": "user", "content": combined_html},
                ],
                temperature=0.0,
                max_tokens=256,
                extra_body={"enable_thinking": False},
            )
        except Exception as e:
            sem.release()
            log.warning("Doc meta extraction error: %s", e)
            return {"supplier_name": None, "bid_total": None,
                    "bid_total_basis": "unknown", "tax_rate": None}
        sem.release()

        raw = (resp.choices[0].message.content or "").strip()
        if "</think>" in raw:
            raw = raw.split("</think>")[-1].strip()
        clean = raw
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            import json as _json
            data = _json.loads(clean)
        except Exception:
            log.warning("Doc meta JSON parse failed: %s", raw[:200])
            data = {}

        return {
            "supplier_name": data.get("supplier_name") or None,
            "bid_total": data.get("bid_total"),
            "bid_total_basis": data.get("bid_total_basis") or "unknown",
            "tax_rate": data.get("tax_rate"),
        }

    # ─── Stage 1: OCR ─────────────────────────────────────────────────────

    @staticmethod
    def _clean_json_text(raw: str) -> str:
        """Strip markdown fences / <think> from a model text response."""
        clean = (raw or "").strip()
        if "</think>" in clean:
            clean = clean.split("</think>")[-1].strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        return clean.strip()

    @staticmethod
    def _mm_text(resp) -> str:
        """Extract concatenated text from a MultiModalConversation response."""
        if isinstance(resp, _StreamedResponse):   # 分片已在 _mm_stream 里拼好
            return resp.text
        text = ""
        if resp.output and resp.output.choices:
            choice = resp.output.choices[0]
            if choice.message and choice.message.content:
                for part in choice.message.content:
                    if hasattr(part, "text"):
                        text += part.text or ""
                    elif isinstance(part, dict) and "text" in part:
                        text += part["text"] or ""
        return text

    def _mm_stream(self, *, api_key: str, model: str, content: list[dict],
                   temperature: float, row_progress_cb=None):
        """Streaming variant — returns an object shaped like a non-stream response.

        为什么必须流式：报价清单的抽取是**长生成**（136 行 CSV ≈ 一两万 token），
        一次性响应会撞上 SDK 的 300s read timeout，而 read timeout 是"多久没收到
        字节"，不是"总共花了多久"。实测远东/上海浦东两份就是这么失败的，且重试
        5 次全部撞同一堵墙——重试一个必然超时的请求纯属浪费（每份白烧 25 分钟）。
        调大超时只是把墙往后挪，下一份更大的文档照样撞；流式让超时取决于
        **token 间隔**而不是生成总长，才是与文档大小无关的解法。

        design/24 B2：`row_progress_cb(n)` 在已转录行数（按累计文本里的换行符数
        近似）每增加 5 行时调用一次——不是每个 chunk 都调用：chunk 是 token 粒度
        的碎片，逐 chunk 上报会让上游（DB commit）被打爆。这是唯一能报"进度"的
        地方：这条长生成没有页数概念，行数是仅有的、单调递增的信号。
        """
        chunks: list[str] = []
        got_any = False
        last_reported = 0
        for resp in dashscope.MultiModalConversation.call(
                api_key=api_key, model=model,
                messages=[{"role": "user", "content": content}],
                temperature=temperature, stream=True, incremental_output=True):
            if getattr(resp, "status_code", 200) != 200:
                return resp                      # 交给调用方按 429/其它错误处理
            got_any = True
            chunks.append(self._mm_text(resp) or "")
            if row_progress_cb is not None:
                current = "".join(chunks).count("\n")
                if current - last_reported >= 5:
                    last_reported = current
                    try:
                        row_progress_cb(current)
                    except Exception:   # noqa: BLE001 — 进度上报绝不能打断识别本身
                        log.warning("row_progress_cb raised, ignoring", exc_info=True)

        if not got_any:
            # 空流当作可重试的失败，而不是"识别出零行"——后者会被下游当成
            # 合法的空报价单，把一次网络问题变成一条业务结论。
            raise ProviderError("VL stream produced no chunks")
        return _StreamedResponse("".join(chunks))

    def _mm_call(self, content: list[dict], model: str,
                 temperature: float = _VISUAL_TEMPERATURE,
                 stream: bool = False, row_progress_cb=None) -> str:
        """Multimodal call with key rotation + retry; returns raw text. Reuses
        the same retry/concurrency infra as _ocr_page.

        `stream` 只对长生成有意义（见 _mm_stream）；短响应（页面角色分类等）
        保持非流式，避免为几十个 token 引入分片重组的失败面。
        """
        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            sem = self._per_key_sem[key]
            sem.acquire()
            try:
                if stream:
                    resp = self._mm_stream(api_key=key, model=model,
                                           content=content, temperature=temperature,
                                           row_progress_cb=row_progress_cb)
                else:
                    resp = dashscope.MultiModalConversation.call(
                        api_key=key, model=model,
                        messages=[{"role": "user", "content": content}],
                        temperature=temperature,
                    )
            except Exception as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_retry_wait(attempt))
                    continue
                raise ProviderError(f"VL call failed after {_MAX_RETRIES} retries: {e}") from e
            sem.release()
            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_retry_wait(attempt))
                    continue
                raise ProviderError(f"VL 429 after {_MAX_RETRIES} retries")
            if resp.status_code != 200:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_retry_wait(attempt))
                    continue
                raise ProviderError(f"VL error {resp.status_code}: {resp.message}")
            return self._mm_text(resp)
        return ""

    @staticmethod
    def _img_part(image_bytes: bytes, max_pixels: int = 2000000) -> dict:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        # qwen3-vl-flash/plus require min_pixels >= 65536 (256×256)
        return {"image": f"data:image/png;base64,{b64}",
                "min_pixels": 65536, "max_pixels": max_pixels}

    def classify_pages_visual(
        self, thumbnails: list[bytes], doc_type: str, *,
        model: str | None = None, prompt_version: str = _VISUAL_PROMPT_VERSION,
        batch_size: int = 10, overlap: int = 1,
        temperature: float = _VISUAL_TEMPERATURE,
        max_pixels: int = _VISUAL_THUMB_MAX_PX,
        file_path: str | None = None,  # accepted for API compat, not used by real provider
    ) -> tuple[list[dict], list[dict]]:
        """Visual page classification via qwen3-vl-flash on low-res thumbnails.

        Batches `batch_size` consecutive pages (with `overlap` context page(s) on
        each side so cross-batch continuation is visible), one MultiModalConversation
        call per batch. Returns (per-page dicts in page order, failures).
        """
        mdl = model or _VISUAL_FLASH_MODEL
        n = len(thumbnails)
        # A档：预计算所有批次规格，并发发出全部 Flash 请求；
        # _mm_call 已有 per-key 信号量限流，max_workers 设为批次数即可。
        batch_specs: list[tuple[int, int, int, int]] = []
        start = 0
        while start < n:
            end = min(start + batch_size, n)
            batch_specs.append((start, end, max(0, start - overlap), min(n, end + overlap)))
            start = end

        def _run_batch(spec: tuple[int, int, int, int]) -> tuple[list[dict], dict | None]:
            _, _, ctx_lo, ctx_hi = spec
            idxs = list(range(ctx_lo, ctx_hi))
            page_nums = [i + 1 for i in idxs]
            # Interleave PAGE N labels with images so the model can unambiguously
            # bind each image to its page number (prevents mis-attribution in batches).
            intro, tail = _build_visual_prompt_parts(doc_type, page_nums)
            content: list[dict] = [{"text": intro}]
            for j, idx in enumerate(idxs):
                content.append({"text": f"PAGE {page_nums[j]}"})
                content.append(self._img_part(thumbnails[idx], max_pixels=max_pixels))
            content.append({"text": tail})
            try:
                raw = self._mm_call(content, mdl, temperature=temperature)
                data = json.loads(self._clean_json_text(raw))
                return data.get("pages") or [], None
            except Exception as e:
                log.warning("visual classify batch %d-%d failed: %s", ctx_lo + 1, ctx_hi, e)
                return [], {"page_range": [ctx_lo + 1, ctx_hi], "error": str(e)}

        by_page: dict[int, dict] = {}
        failures: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(batch_specs)) as ex:
            for pages, err in ex.map(_run_batch, batch_specs):
                if err:
                    failures.append(err)
                    continue
                for entry in pages:
                    try:
                        p = int(entry.get("page", 0))
                    except (TypeError, ValueError):
                        continue
                    if p < 1 or p > n:
                        continue
                    # 重叠页：取 confidence 更高者
                    prev = by_page.get(p)
                    if prev is None or float(entry.get("confidence", 0)) > float(prev.get("confidence", 0)):
                        entry["source"] = "flash"
                        by_page[p] = entry
        out = [by_page.get(p, {"page": p, "role": "unknown", "confidence": 0.0,
                               "contains_table": False, "orientation": 0,
                               "continues_from_page": None, "mixed_content": False,
                               "evidence": ["flash 未返回该页"], "source": "flash"})
               for p in range(1, n + 1)]
        return out, failures

    def review_pages_visual(
        self, page_image: bytes, neighbor_thumbs: list[bytes],
        flash_result: dict, page_no: int, *,
        chain_context: list[dict] | None = None,
        model: str | None = None,
    ) -> dict:
        """Re-adjudicate one low-confidence page with qwen3-vl-plus on the
        high-res image + neighbor thumbnails. Returns a single page dict."""
        mdl = model or _VISUAL_PLUS_MODEL
        chain_note = (
            f"前序已确认页分类（判断续表关系用）：\n"
            f"{json.dumps(chain_context, ensure_ascii=False)}\n"
            if chain_context else ""
        )
        content = [self._img_part(page_image, max_pixels=4000000)]
        content += [self._img_part(t) for t in neighbor_thumbs]
        content.append({"text": _VISUAL_REVIEW_SUFFIX.format(
            flash_json=json.dumps(flash_result, ensure_ascii=False),
            chain_context_note=chain_note, page=page_no)})
        try:
            raw = self._mm_call(content, mdl)
            data = json.loads(self._clean_json_text(raw))
            if isinstance(data, dict) and "pages" in data and data["pages"]:
                data = data["pages"][0]
            data["source"] = "plus"
            data["page"] = page_no
            return data
        except Exception as e:
            log.warning("visual review page %d failed: %s", page_no, e)
            fallback = dict(flash_result)
            fallback["source"] = "plus_failed"
            return fallback

    def classify_document_kind(self, page_images: list[bytes], *, model: str | None = None) -> dict:
        """design/29 §3 Tier 1.5——扫描件招标/投标判定，用便宜的 flash 模型
        （跟 classify_pages_visual 同一档，不是逐行抽取用的 plus）。这是
        "这份文档整体是什么"的粗判断，不是页面角色分类（`classify_pages_visual`）
        ——不复用那条 prompt，那条刻意"不得依赖供应商名称做判断"，这里恰恰
        要读封面/投标函上的招标编号/投标单位这些内容信号，两者判据方向相反，
        不能共用一份 prompt。

        2026-08-21 实测修正：只送第一页缩略图（2M 像素预算）实测 0/7——不是
        模型不会读，是这一版给的信息量不够。机电投标封面惯例上会原样重印
        招标方自己的封面模板（同时印"招标单位"和"投标单位"两个字段），模型
        只看得到封面时，容易把"看到招标单位字样"误判成"这是招标文件"，看不
        到后面"投标函""授权书"这些不会跟模板混淆的内容来纠正。改成送前
        `page_images` 张原生分辨率页图（不再用缩略图压缩）+ 直接点破这个坑
        的提示词，同一批真实语料复测 **8/8**（7 份投标 + 1 份招标全对）。

        Returns 原始解析后的 dict：{"doc_type": "tender"|"bid"|"uncertain",
        "project_name_hint": str, "supplier_name_hint": str, "evidence": [...]}
        ——解析失败或调用异常时返回 doc_type="uncertain"，不抛异常（design/28
        Tier 0/1 同一条"失败不拖垮主线"约定，判不出来诚实答不确定）。"""
        mdl = model or _VISUAL_FLASH_MODEL
        content = [self._img_part(img, max_pixels=_CLASSIFY_DOC_KIND_MAX_PX) for img in page_images]
        content.append({"text": _CLASSIFY_DOC_KIND_PROMPT})
        try:
            raw = self._mm_call(content, mdl, temperature=_VISUAL_TEMPERATURE)
            data = json.loads(self._clean_json_text(raw))
            doc_type = data.get("doc_type")
            if doc_type not in ("tender", "bid", "uncertain"):
                doc_type = "uncertain"
            return {
                "doc_type": doc_type,
                "project_name_hint": (data.get("project_name_hint") or "").strip(),
                "supplier_name_hint": (data.get("supplier_name_hint") or "").strip(),
                "evidence": data.get("evidence") or [],
            }
        except Exception as e:
            log.warning("classify_document_kind failed: %s", e)
            return {"doc_type": "uncertain", "project_name_hint": "", "supplier_name_hint": "",
                    "evidence": [f"调用/解析失败：{e}"]}

    def vl_extract_csv(
        self,
        images: list[bytes],
        prompt: str,
        *,
        model: str | None = None,
        labels: list[str] | None = None,
        max_pixels: int = 8_000_000,
        temperature: float = 0.0,
        row_progress_cb=None,
    ) -> str:
        """VL-direct：一次调用送 N 张页图，返回模型原始文本（CSV）。

        整份送而非逐页：续页表头在前页、重复副本要跨页才看得见、序号连续性也只有
        整份才能校验。

        `labels` 用于在每张图前插一段文本标签（如 PAGE_3_ROT_90）。方向预检必须交错
        标签——不交错模型会串页（视觉分类稳定性 v4 的教训）。

        复用 `_mm_call` 的多 key 轮换与限流；**不解析、不清洗**，原样返回给调用方，
        解析规则属于识别器而不是 provider。

        `row_progress_cb`（design/24 B2，可选）：仅在 `stream=True` 的长生成路径
        （报价清单抽取，见 `_mm_stream`）有意义——方向预检等短响应调用方不传，
        不受影响。callers（如 pipeline.py 的 `_vl_call`）按需传入。
        """
        mdl = model or os.getenv("DASHSCOPE_QUOTE_VL_MODEL", _VISUAL_PLUS_MODEL)
        content: list[dict] = []
        for i, img in enumerate(images):
            if labels and i < len(labels):
                content.append({"text": labels[i]})
            content.append(self._img_part(img, max_pixels=max_pixels))
        content.append({"text": prompt})
        # 流式：整份报价清单是长生成，非流式会撞 SDK 的 300s read timeout（见 _mm_stream）。
        raw = self._mm_call(content, mdl, temperature=temperature, stream=True,
                            row_progress_cb=row_progress_cb)
        return (raw or "").replace("```csv", "").replace("```", "").strip()

    def _extract_structured_experimental(
        self,
        page_image: bytes,
        prompt: str,
        *,
        model: str | None = None,
        max_pixels: int = 8_000_000,
        temperature: float = 0.0,
    ) -> tuple[dict, str, int]:
        """Experimental VL-direct page extraction.

        This bypasses Stage-1 OCR HTML and asks a visual-language model to
        return the same JSON shape consumed by the quote/tender post-processors.
        It is intentionally not used by the production recognizer yet; scripts
        can call it to compare VL->JSON against OCR HTML->LLM.
        """
        mdl = model or os.getenv("DASHSCOPE_VL_STRUCTURED_MODEL", _VISUAL_PLUS_MODEL)
        content = [
            self._img_part(page_image, max_pixels=max_pixels),
            {"text": prompt},
        ]
        raw = self._mm_call(content, mdl, temperature=temperature)
        clean = self._clean_json_text(raw)
        try:
            return json.loads(clean), raw, 0
        except Exception as exc:
            raise ProviderError(
                f"VL structured JSON parse failed: {exc}\nRaw: {raw[:300]}"
            ) from exc

    def _ocr_page(self, page_bytes: bytes) -> tuple[str, int]:
        """Qwen-VL-OCR table_parsing on one page. Retries on 429 / connection errors."""
        # base64 编码延后到拿到限流信号量之后再做（且只做一次），避免大量等待线程
        # 各自持有一份 ~1.33x 的 base64/data_uri 副本拖高内存峰值。
        data_uri: str | None = None

        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            sem = self._per_key_sem[key]
            sem.acquire()
            if data_uri is None:
                data_uri = f"data:image/png;base64,{base64.b64encode(page_bytes).decode('ascii')}"
            try:
                resp = dashscope.MultiModalConversation.call(
                    api_key=key,
                    model=self.ocr_model,
                    messages=[{
                        "role": "user",
                        "content": [{
                            "image": data_uri,
                            "min_pixels": 3136,
                            "max_pixels": 8388608,
                        }],
                    }],
                    ocr_options={"task": "table_parsing"},
                )
            except Exception as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(attempt)
                    log.warning("OCR connection error (attempt %d/%d), retry in %.1fs: %s",
                                attempt + 1, _MAX_RETRIES, wait, e)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR call failed after {_MAX_RETRIES} retries: {e}") from e
            sem.release()

            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(attempt)
                    log.warning("OCR 429 rate limited (attempt %d/%d), retry in %.1fs",
                                attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR 429 after {_MAX_RETRIES} retries")

            if resp.status_code != 200:
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(attempt)
                    log.warning("OCR %d error (attempt %d/%d), retry in %.1fs: %s",
                                resp.status_code, attempt + 1, _MAX_RETRIES, wait, resp.message)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR error {resp.status_code}: {resp.message}")

            text = ""
            if resp.output and resp.output.choices:
                choice = resp.output.choices[0]
                if choice.message and choice.message.content:
                    for part in choice.message.content:
                        if hasattr(part, "text"):
                            text += part.text
                        elif isinstance(part, dict) and "text" in part:
                            text += part["text"]

            tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
            return text, tokens

        return "", 0

    # ─── Stage 2: Text LLM ────────────────────────────────────────────────

    def _llm_parse(self, html: str, doc_type: str) -> tuple[dict, str, int]:
        """Parse OCR HTML into structured JSON via Stage-2 LLM."""
        s2_prompt = _QUOTE_S2_PROMPT if doc_type == "quote" else _TENDER_S2_PROMPT
        return self._llm_call_json(s2_prompt, html)

    def _llm_call_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        enable_thinking: bool = False,
    ) -> tuple[dict, str, int]:
        """Call the text LLM with retry; return (parsed_dict, raw_text, tokens)."""
        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            client = self._get_client(key)
            sem = self._per_key_sem[key]
            sem.acquire()
            try:
                resp = client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.0,  # 降低抽取非确定性(召回波动);见 design/05 §9.1
                    max_tokens=8192,
                    extra_body={"enable_thinking": enable_thinking},
                )
            except openai.RateLimitError as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(attempt)
                    log.warning("LLM 429 rate limited (attempt %d/%d), retry in %.1fs",
                                attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"LLM 429 after {_MAX_RETRIES} retries") from e
            except Exception as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(attempt)
                    log.warning("LLM connection error (attempt %d/%d), retry in %.1fs: %s",
                                attempt + 1, _MAX_RETRIES, wait, e)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"LLM call failed after {_MAX_RETRIES} retries: {e}") from e
            sem.release()

            raw = (resp.choices[0].message.content or "").strip()
            tokens = resp.usage.total_tokens if resp.usage else 0

            clean = raw
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)
            if "</think>" in clean:
                clean = clean.split("</think>")[-1].strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)

            try:
                data = json.loads(clean)
                return data, raw, tokens
            except (json.JSONDecodeError, ValueError) as e:
                if attempt < _MAX_RETRIES - 1:
                    log.warning("LLM JSON parse failed (attempt %d/%d): %s — raw: %s",
                                attempt + 1, _MAX_RETRIES, e, raw[:200])
                    continue
                raise ProviderError(
                    f"LLM JSON parse failed: {e}\nRaw: {raw[:300]}"
                ) from e

        raise ProviderError("LLM extraction failed after all retries")

    # ─── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _guess_doc_type(prompt: str) -> str:
        if "报价" in prompt or "quote" in prompt.lower():
            return "quote"
        return "tender"
