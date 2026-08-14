"""tier1_signals.py — Tier 1 信号融合：识别跑完之后，从产物本身看它更像招标
还是投标。design/28 §3。

跟 Tier 0（document_classify.py）的分工边界：Tier 0 在文件字节层面判定，
零模型调用；Tier 1 在识别已经跑完之后判定——识别本来就要跑（无论当时按
"招标"还是"投标"哪个假设送进流水线），这层不额外花钱，只是把产物本身的
形状/内容当证据用。

**跟 design/28 §3 原文的一处偏差，记录在这里而不是悄悄改掉**：原文列的招标
侧封面标量是"招标编号/招标人"。核实 `paddle_doc_meta.py`/`vl_tender.py` 后
发现整条识别链路目前**没有"招标人"（发标单位名）这个字段**，只有
`project_code`/`project_name`（提示词里标"项目编号/招标编号"）。本模块用
`project_code`/`project_name` 顶替"招标编号/招标人"这一档证据；等哪天真的
补上招标人抽取，接在 `extract_tier1_signals_from_job_result` 里改，不动
这个模块对外的融合契约（`Tier1Signals` 字段名不变）。

三路证据、加权融合成一个 [-1, 1] 的分数，不是任一条单一证据直接拍板：
  - 价格列填充率（结构性，权重最高——招标侧的规范槽位表压根不含价格字段，
    这条证据不依赖任何一次 LLM 抽取是否抽对了）
  - 投标方名称是否抽到（`_doc_meta.supplier_name`，报价侧独有）
  - 招标侧封面标量是否抽到（`project_code`/`project_name`）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier1Verdict = Literal["tender", "bid", "uncertain"]
Tier1Confidence = Literal["strong", "moderate", "ambiguous"]

# 价格列字段名——报价侧 items 才会出现这些 key；招标侧规范槽位表
# （TENDER_SLOTS，见 vl_tender.py）结构性地不含任何价格字段，所以对招标
# 产物算这个比例天然得 0.0，这正是我们想要的证据，不是缺陷。
_PRICE_FIELDS = (
    "unit_price", "unit_price_excl_tax", "unit_price_incl_tax",
    "total_price", "total_price_excl_tax", "total_price_incl_tax",
)

# 三路证据的权重，和为 1.0。价格列填充率权重最高——它是结构性信号，不依赖
# 任何一次 LLM 文本抽取抽没抽对；封面标量两路权重较低且相等，因为两者都
# 依赖识别侧的文本/视觉抽取质量，出错率天然更高。
_WEIGHT_PRICE = 0.5
_WEIGHT_SUPPLIER = 0.25
_WEIGHT_COVER_TENDER = 0.25

# 融合分数 → 判定的分档阈值。跟 Tier 0 的 90% 门槛一样，是"明显偏向一侧"
# 的整数取值，不是从某次实测反推出来的精确拟合。
_STRONG_THRESHOLD = 0.5
_MODERATE_THRESHOLD = 0.15


@dataclass
class Tier1Signals:
    """融合函数的输入契约——跟"怎么从 job.result 里读出这些值"解耦，方便
    单独对融合数学写确定性单测，不用每次都造一份完整的识别产物。"""

    price_parse_rate: float             # items 里价格字段有值的行占比，[0,1]
    supplier_name_present: bool | None  # True/False=抽取过且看到了/没看到；None=这条证据本来就不适用（既不是报价产物也没做过这次抽取）
    cover_tender_fields_present: bool   # project_code/project_name 任一非空
    row_count: int


@dataclass
class Tier1Result:
    verdict: Tier1Verdict
    confidence: Tier1Confidence
    score: float          # 融合后的原始分数，[-1, 1]，负值偏招标、正值偏投标
    signals: Tier1Signals
    reason: str


def _price_signal(rate: float) -> float:
    """线性映射：0 填充率 → -1（强招标证据），100% 填充率 → +1（强投标证据）。"""
    return max(-1.0, min(1.0, 2 * rate - 1))


def fuse_tier1_signals(signals: Tier1Signals) -> Tier1Result:
    """纯函数，不做任何 I/O——所有输入都已经是结构化证据。"""
    price_score = _price_signal(signals.price_parse_rate)

    if signals.supplier_name_present is None:
        supplier_score = 0.0
    elif signals.supplier_name_present:
        supplier_score = 1.0
    else:
        supplier_score = -0.3  # 抽取跑过但没看到供应商名，弱投标反证据，不是强证据——
        # 抽取本身失手是常见情况，不能跟"这份文档结构上就不含供应商信息"划等号

    cover_score = -1.0 if signals.cover_tender_fields_present else 0.0

    score = (_WEIGHT_PRICE * price_score
             + _WEIGHT_SUPPLIER * supplier_score
             + _WEIGHT_COVER_TENDER * cover_score)

    if signals.row_count == 0:
        return Tier1Result(
            verdict="uncertain", confidence="ambiguous", score=0.0, signals=signals,
            reason="识别产物零行，没有可供判定的证据。",
        )

    evidence = (f"价格列填充率={signals.price_parse_rate:.0%}、"
               f"供应商名={signals.supplier_name_present}、"
               f"招标封面标量={signals.cover_tender_fields_present}、"
               f"融合分数={score:+.2f}")

    if score >= _STRONG_THRESHOLD:
        return Tier1Result("bid", "strong", score, signals, f"{evidence}——明显偏投标。")
    if score >= _MODERATE_THRESHOLD:
        return Tier1Result("bid", "moderate", score, signals, f"{evidence}——偏投标，但不够强。")
    if score <= -_STRONG_THRESHOLD:
        return Tier1Result("tender", "strong", score, signals, f"{evidence}——明显偏招标。")
    if score <= -_MODERATE_THRESHOLD:
        return Tier1Result("tender", "moderate", score, signals, f"{evidence}——偏招标，但不够强。")
    return Tier1Result("uncertain", "ambiguous", score, signals, f"{evidence}——证据不够一边倒，交给下一级判据。")


def _has_value(v) -> bool:
    """跟 document_classify._looks_filled 同一条原则："0" 算有值，真空/None
    才算没有——字面零不等于没填过。"""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def extract_tier1_signals_from_job_result(job_result: dict) -> Tier1Signals:
    """从一份真实的 `job.result`（`document_ingestion.py` 产出的形状，招标/
    报价两种 shape 都要能读）里抽出融合函数要的三路证据。这是唯一需要知道
    `job.result` 具体 schema 的地方——schema 漂移只用改这一个函数。
    """
    items = job_result.get("items") or []
    row_count = len(items)

    priced_rows = sum(
        1 for it in items
        if isinstance(it, dict) and any(_has_value(it.get(f)) for f in _PRICE_FIELDS)
    )
    price_parse_rate = (priced_rows / row_count) if row_count else 0.0

    doc_meta = job_result.get("_doc_meta") or {}
    # 供应商名这个 key 报价侧才可能出现（顶层 supplier_name 或 _doc_meta.supplier_name）；
    # 招标侧产物压根没有这个字段，此时这条证据"不适用"，不是"抽了没抽到"。
    has_supplier_field = "supplier_name" in doc_meta or "supplier_name" in job_result
    if has_supplier_field:
        supplier_name = doc_meta.get("supplier_name") or job_result.get("supplier_name") or ""
        supplier_name_present = bool(str(supplier_name).strip())
    else:
        supplier_name_present = None

    cover_tender_fields_present = bool(
        str(job_result.get("project_code") or "").strip()
        or str(job_result.get("project_name") or "").strip()
    )

    return Tier1Signals(
        price_parse_rate=price_parse_rate,
        supplier_name_present=supplier_name_present,
        cover_tender_fields_present=cover_tender_fields_present,
        row_count=row_count,
    )


def classify_tier1(job_result: dict) -> Tier1Result:
    """便捷入口：读 job.result → 抽信号 → 融合判定，一步到位。"""
    return fuse_tier1_signals(extract_tier1_signals_from_job_result(job_result))
