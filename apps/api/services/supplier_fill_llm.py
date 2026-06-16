"""supplier_fill_llm.py — LLM 供应商视角填采购清单代理。

每家供应商一个代理：给它 90 个采购清单锚点 + 这一家的报价(含 Top-K 候选) +
高置信标注，让 LLM 站在供应商视角判断每条报价落到哪个锚点(或 residue)。

核心原则：**LLM 只提议，代码做裁决**。validate() 是安全核心——纯函数、无 DB、
无网络，把所有反幻觉保证钉死在与模型行为无关的代码里：
  (a) quote_id 必须属于本供应商已加载报价
  (b) anchor_seq 必须真实存在
  (c) 一条报价最多消费一次(v1 不支持 split)
  (d) 价格永远取自真实 Quote 行；LLM 价仅作 mismatch tripwire
  (e) 聚合：同锚点多条 → 一致性校验 + agg_total/agg_qty 重算
  (f) 丢弃/无主 → residue + 审计；residue 中 best cos≥LOW_CONF 计入 residue_high_cos
  (g) canonical 硬阻断兜底：valve_type/DN/PN 冲突 → quoted 降级 pending

Phase 4 在本模块追加 attach_topk / build_prompt / call_llm / fill_one_supplier
(worker 纯数据，不碰 DB)。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from apps.api.services.canonical import (
    canonical_match_score,
    extract_valve_canonical,
    valve_type_compatible,
)

log = logging.getLogger(__name__)

# 价格交叉校验容差(与 quote_fact.apply_arithmetic_validation 一致)
PRICE_TOL = 0.05
# residue 中 best cos ≥ 此阈值视为「embedding 自信但 LLM 漏」(与 anchor_match.LOW_CONF 一致)
RESIDUE_HIGH_COS = 0.70

# Tier-1 代码直判 align 的门槛(canonical 完全一致 + 高 cos)
TIER1_COS = 0.85
# Tier-3 thinking 触发：候选过近、或高金额
TIER3_CLOSE_GAP = 0.05
TIER3_HIGH_AMOUNT = 50000.0

# Tier-2 默认模型(绝不用 qwen3.6-flash 做最终填表裁判)
DEFAULT_FILL_MODEL = "qwen-plus"

_VALID_STATUS = {"quoted", "pending", "aggregated", "excluded"}


# ─── 数据结构(纯数据，由路由主线程从 DB 构造后传给 worker) ──────────────────

@dataclass
class SupplierQuoteRow:
    """一条供应商报价(纯数据，含标准化前的原始表达供 LLM「像人一样看」)。"""
    quote_id: int
    supplier_id: int
    raw_material: str = ""        # 原始品名(标准化前)
    raw_spec: str = ""            # 原始规格
    raw_unit: str = ""
    raw_remark: str = ""
    material: str = ""            # 标准名
    spec: str = ""
    unit: str = ""
    qty: float | None = None
    unit_price: float | None = None
    total_price: float | None = None
    material_type: str = ""       # 材质 (不锈钢/球墨铸铁…)
    # Layer 1 OCR correction (raw text preserved above, corrected name here)
    normalized_material: str = ""
    ocr_correction_reason: str = ""
    canonical: dict = field(default_factory=dict)
    topk: list[tuple[int, float]] = field(default_factory=list)  # [(anchor_seq, cos), ...]
    candidates: list = field(default_factory=list)  # list[AnchorCandidate] from match_anchors_wide

    def best_cos(self) -> float:
        if self.candidates:
            return max((c.cosine for c in self.candidates), default=0.0)
        return max((c for _seq, c in self.topk), default=0.0)


@dataclass
class AnchorView:
    """采购清单锚点投影(用于 prompt、Top-K 召回与校验)。"""
    seq: int
    name: str = ""
    spec: str = ""
    pressure: str = ""
    unit: str = ""
    qty: float | None = None
    canonical: dict = field(default_factory=dict)

    def material_text(self) -> str:
        # AnchorView 仅用于 worker 内的 dn/canonical 评分；锚点向量由路由层预先
        # 用完整 TenderAnchor 算好并经 anchor_vecs 传入，故此处材质文本可留空。
        return str(self.canonical.get("material") or "")


@dataclass
class FillCell:
    """一个 (supplier, anchor) 的最终裁决，直接映射成一个 BidAlignmentItem。"""
    anchor_seq: int
    supplier_id: int
    action: str                  # align | pending | exclude
    status: str                  # quoted | aggregated | pending | excluded
    quote_id: int                # 代表行
    unit_price: float | None = None
    qty: float | None = None
    total_price: float | None = None
    agg_total: float | None = None
    agg_qty: float | None = None
    aggregated_quote_ids: list[int] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class SupplierFillResult:
    supplier_id: int
    cells: list[FillCell] = field(default_factory=list)
    residue_quote_ids: list[int] = field(default_factory=list)
    residue_high_cos: int = 0
    dropped: list[dict] = field(default_factory=list)
    tokens_used: int = 0
    duration_ms: int = 0
    error: str = ""

    def counts(self) -> dict:
        c = {"quoted": 0, "aggregated": 0, "pending": 0, "excluded": 0}
        for cell in self.cells:
            c[cell.status] = c.get(cell.status, 0) + 1
        return c


# ─── 内部辅助 ─────────────────────────────────────────────────────────────────

def _action_for_status(status: str) -> str:
    if status in ("quoted", "aggregated"):
        return "align"
    if status == "excluded":
        return "exclude"
    return "pending"


def _coerce_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_total(row: SupplierQuoteRow) -> float | None:
    """每行合价：优先真实 total_price，否则 unit_price×qty。"""
    if row.total_price is not None:
        return row.total_price
    if row.unit_price is not None and row.qty is not None:
        return round(row.unit_price * row.qty, 4)
    return None


def _ocr_correction_type_plausible(effective_from: str, corr_to: str, anchor_vt: str) -> bool:
    """True if the OCR correction from→to is a plausible character-error fix for anchor_vt.

    Rules:
    1. corr_to must extract to exactly anchor_vt.
    2. effective_from must extract to a valve_type that is the same as, or a parent class of,
       anchor_vt (i.e., anchor_vt contains from_vt as a substring).
       Allows: 止回阀→橡胶瓣止回阀 (narrowing within the same family, plausible OCR).
       Rejects: 流量测试→闸阀, 真空破坏器→减压阀组 (completely different product classes).
    3. If effective_from has no recognizable valve_type, the correction is unverifiable → rejected.
    """
    if not effective_from or not corr_to or not anchor_vt:
        return False
    to_fresh = extract_valve_canonical(corr_to, "")
    if to_fresh.get("valve_type") != anchor_vt:
        return False
    from_fresh = extract_valve_canonical(effective_from, "")
    from_vt = from_fresh.get("valve_type")
    if from_vt is None:
        return False
    return from_vt == anchor_vt or from_vt in anchor_vt


# ─── 校验器(安全核心) ─────────────────────────────────────────────────────────

def validate(
    raw_llm: dict,
    anchors: list[AnchorView],
    rows: list[SupplierQuoteRow],
    allow_split: bool = False,
) -> SupplierFillResult:
    """把 LLM 的 assignments 裁决成可落库的 FillCell 列表(纯函数，无 DB)。

    raw_llm: {"assignments": [{quote_id, anchor_seq, status, confidence, reason,
              llm_unit_price?}, ...]}。畸形/缺失 → 当作空 assignments(全部 residue)。
    """
    supplier_id = rows[0].supplier_id if rows else 0
    result = SupplierFillResult(supplier_id=supplier_id)

    qid_to_row: dict[int, SupplierQuoteRow] = {r.quote_id: r for r in rows}
    seq_to_anchor: dict[int, AnchorView] = {int(a.seq): a for a in anchors}

    raw_assignments = raw_llm.get("assignments") if isinstance(raw_llm, dict) else None
    if not isinstance(raw_assignments, list):
        raw_assignments = []

    # 规范化 + 按 confidence 降序(先到先得)
    norm: list[dict] = []
    for a in raw_assignments:
        if not isinstance(a, dict):
            continue
        norm.append({
            "quote_id": _coerce_int(a.get("quote_id")),
            "anchor_seq": _coerce_int(a.get("anchor_seq")),
            "status": str(a.get("status") or "").strip().lower(),
            "confidence": _coerce_float(a.get("confidence")) or 0.0,
            "reason": str(a.get("reason") or "").strip(),
            "llm_unit_price": _coerce_float(a.get("llm_unit_price")),
        })
    norm.sort(key=lambda x: x["confidence"], reverse=True)

    # 第一轮：逐 assignment 裁决(规则 a-d, g)，产出 accepted 临时记录
    consumed: set[int] = set()
    accepted: list[dict] = []  # {row, anchor, status, action, confidence, reason, flags}

    for a in norm:
        qid = a["quote_id"]
        seq = a["anchor_seq"]
        # (a) quote_id 归属
        if qid is None or qid not in qid_to_row:
            result.dropped.append({"quote_id": qid, "anchor_seq": seq, "reason": "unknown_quote_id"})
            continue
        # (b) anchor_seq 存在
        if seq is None or seq not in seq_to_anchor:
            result.dropped.append({"quote_id": qid, "anchor_seq": seq, "reason": "unknown_anchor_seq"})
            continue
        # (c) 单次消费(v1 不支持 split)
        if qid in consumed:
            result.dropped.append({"quote_id": qid, "anchor_seq": seq, "reason": "duplicate_quote_id"})
            continue

        row = qid_to_row[qid]

        # (b2) model_invalid_assignment: seq not in wide candidate pool
        if row.candidates:
            _cand_seqs = {c.seq for c in row.candidates}
            if seq not in _cand_seqs:
                result.dropped.append({"quote_id": qid, "anchor_seq": seq, "reason": "model_invalid_assignment"})
                continue

        anchor = seq_to_anchor[seq]
        status = a["status"] if a["status"] in _VALID_STATUS else "pending"
        flags: list[str] = []

        # risky candidate downgrade: selected risky tier → force pending,
        # UNLESS it's risky_canonical_conflict AND a fresh canonical recheck
        # proves the pair safe (exact or family-compatible, score ≥0.75).
        # Rescue scope is intentionally narrow:
        #   risky_canonical_conflict  → may be a stale-rule false-kill → rescue ok
        #   risky_dn_mismatch         → genuine DN conflict → never rescue
        #   risky_low_similarity      → low cos → never rescue via canonical alone
        if row.candidates:
            for _c in row.candidates:
                if _c.seq == seq and _c.tier != "safe":
                    if _c.tier == "risky_canonical_conflict":
                        _fresh = canonical_match_score(anchor.canonical or {}, row.canonical or {})
                        if _fresh >= 0.75:
                            # >=0.75 ⇒ valve_type exact/family-compatible AND no
                            # DN/PN conflict. 0.5 wildcard is NOT rescued.
                            if "family_normalized_verified" not in flags:
                                flags.append("family_normalized_verified")
                        else:
                            flags.append(f"risky_candidate:{_c.tier}")
                            if status in ("quoted", "aggregated"):
                                status = "pending"
                    else:
                        # risky_dn_mismatch / risky_low_similarity: downgrade always
                        flags.append(f"risky_candidate:{_c.tier}")
                        if status in ("quoted", "aggregated"):
                            status = "pending"
                    break

        # (d) 价格完整性：永不信 LLM 价；不符则降级 pending
        llm_price = a["llm_unit_price"]
        if (llm_price is not None and row.unit_price is not None and row.unit_price > 0
                and abs(llm_price - row.unit_price) / row.unit_price > PRICE_TOL):
            flags.append("price_mismatch")
            if status in ("quoted", "aggregated"):
                status = "pending"

        # (g) canonical 硬阻断兜底
        if status in ("quoted", "aggregated"):
            cscore = canonical_match_score(anchor.canonical or {}, row.canonical or {})
            if cscore == 0.0:
                flags.append("canonical_conflict")
                status = "pending"

        # (g2) Fresh valve-type conflict gate.
        # row.canonical may be stale (valve_type=None) when the DB was populated before
        # "真空破坏器" / "流量测试" were added to _VALVE_TYPES.  Re-extract from raw text
        # so the gate fires even on old data.
        if status in ("quoted", "aggregated"):
            _anchor_vt = (anchor.canonical or {}).get("valve_type")
            if _anchor_vt:
                _qt = " ".join(filter(None, [row.normalized_material, row.raw_material, row.material]))
                if _qt:
                    _fresh = extract_valve_canonical(_qt, row.raw_spec or row.spec)
                    _q_vt = _fresh.get("valve_type")
                    if _q_vt and not valve_type_compatible(_anchor_vt, _q_vt):
                        flags.append(f"valve_type_conflict:{_q_vt}")
                        status = "pending"
                        result.dropped.append({
                            "anchor_seq": seq, "quote_id": qid,
                            "reason": "valve_type_conflict",
                            "anchor_vt": _anchor_vt, "quote_vt": _q_vt,
                            "quote_text": _qt[:80],
                        })

        consumed.add(qid)
        accepted.append({
            "row": row, "anchor": anchor, "seq": seq, "status": status,
            "confidence": a["confidence"], "reason": a["reason"], "flags": flags,
        })

    # 第二轮：按 anchor_seq 分组，聚合一致性 + 重算(规则 e)
    by_seq: dict[int, list[dict]] = {}
    for rec in accepted:
        by_seq.setdefault(rec["seq"], []).append(rec)

    for seq, recs in by_seq.items():
        aligned = [r for r in recs if r["status"] in ("quoted", "aggregated")]
        pendings = [r for r in recs if r["status"] == "pending"]
        excludeds = [r for r in recs if r["status"] == "excluded"]

        # 聚合一致性：以最高置信 aligned 成员为种子，与种子 canonical 冲突的剔出转 pending
        if len(aligned) >= 2:
            aligned.sort(key=lambda r: r["confidence"], reverse=True)
            seed = aligned[0]
            consistent = [seed]
            for member in aligned[1:]:
                if canonical_match_score(seed["row"].canonical or {}, member["row"].canonical or {}) == 0.0:
                    member["flags"].append("agg_conflict")
                    pendings.append(member)
                else:
                    consistent.append(member)
            aligned = consistent

        # aligned → 一个聚合或单条 align cell
        if len(aligned) >= 2:
            members = [r["row"] for r in aligned]
            agg_total = sum(t for t in (_row_total(m) for m in members) if t is not None)
            agg_qty = sum(q for q in (m.qty for m in members) if q is not None)
            rep = max(aligned, key=lambda r: _row_total(r["row"]) or 0.0)
            rep_row = rep["row"]
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id, action="align", status="aggregated",
                quote_id=rep_row.quote_id, unit_price=rep_row.unit_price, qty=rep_row.qty,
                total_price=_row_total(rep_row),
                agg_total=round(agg_total, 4), agg_qty=agg_qty or None,
                aggregated_quote_ids=[m.quote_id for m in members],
                confidence=min(r["confidence"] for r in aligned),
                reason=rep["reason"],
                flags=sorted({f for r in aligned for f in r["flags"]}),
            ))
        elif len(aligned) == 1:
            r = aligned[0]
            row = r["row"]
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id, action="align", status="quoted",
                quote_id=row.quote_id, unit_price=row.unit_price, qty=row.qty,
                total_price=_row_total(row), confidence=r["confidence"],
                reason=r["reason"], flags=r["flags"],
            ))

        # pending / excluded → 各自成 cell
        for r in pendings:
            row = r["row"]
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id, action="pending", status="pending",
                quote_id=row.quote_id, unit_price=row.unit_price, qty=row.qty,
                total_price=_row_total(row), confidence=r["confidence"],
                reason=r["reason"], flags=r["flags"],
            ))
        for r in excludeds:
            row = r["row"]
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id, action="exclude", status="excluded",
                quote_id=row.quote_id, unit_price=row.unit_price, qty=row.qty,
                total_price=_row_total(row), confidence=r["confidence"],
                reason=r["reason"], flags=r["flags"],
            ))

    # (f) residue：未消费的报价
    for row in rows:
        if row.quote_id not in consumed:
            result.residue_quote_ids.append(row.quote_id)
            if row.best_cos() >= RESIDUE_HIGH_COS:
                result.residue_high_cos += 1

    if result.dropped:
        log.warning("supplier_fill validate: supplier=%s dropped=%d %s",
                    supplier_id, len(result.dropped), result.dropped[:5])

    return result


# ─── Top-K 召回(worker 内，网络调用 embedding，不碰 DB) ────────────────────────

def attach_topk(
    anchors: list[AnchorView],
    rows: list[SupplierQuoteRow],
    client: Any | None = None,
    k: int = 3,
    anchor_vecs: list[list[float]] | None = None,
) -> None:
    """为每条报价填充 row.topk = [(anchor_seq, cos), ...]。

    复用 anchor_match.match_anchors_topk(embedding + DN + canonical 硬过滤)。已有
    topk 的行跳过(便于测试/预计算)。anchor_vecs 由路由层预算好以免重复 embed。
    """
    from apps.api.services.anchor_match import match_anchors_topk, _dn_of

    pending = [r for r in rows if not r.topk]
    if not pending:
        return

    idx_to_seq = {i: int(a.seq) for i, a in enumerate(anchors)}
    quote_texts = [f"{(r.material or r.raw_material)} {(r.spec or r.raw_spec)}".strip() for r in pending]
    quote_dns = [_dn_of(r.spec or r.raw_spec) for r in pending]
    quote_canonicals = [r.canonical or {} for r in pending]

    cands = match_anchors_topk(
        anchors, pending, quote_texts, quote_dns,
        client=client, quote_canonicals=quote_canonicals, k=k, anchor_vecs=anchor_vecs,
    )
    for r, cand in zip(pending, cands):
        r.topk = [(idx_to_seq[ai], cos) for ai, cos, _cscore in cand]


# ─── Wide recall (safe + risky tiers) ───────────────────────────────────────

def attach_wide_candidates(
    anchors: list[AnchorView],
    rows: list[SupplierQuoteRow],
    client: Any | None = None,
    k_safe: int = 5,
    k_risky: int = 5,
    anchor_vecs: list[list[float]] | None = None,
) -> None:
    """Wide Top-(k_safe+k_risky) recall; populates row.candidates and backfills row.topk.

    Unlike attach_topk which hard-blocks risky items, this exposes risky candidates to
    LLM (which then can only force them to pending). Safe candidates backfill row.topk
    for Tier-1 and hints in prompt.
    """
    from apps.api.services.anchor_match import match_anchors_wide, _dn_of

    pending = [r for r in rows if not r.candidates]
    if not pending:
        return

    quote_texts = [f"{(r.material or r.raw_material)} {(r.spec or r.raw_spec)}".strip() for r in pending]
    quote_dns = [_dn_of(r.spec or r.raw_spec) for r in pending]
    quote_canonicals = [r.canonical or {} for r in pending]

    cands_per_row = match_anchors_wide(
        anchors, pending, quote_texts, quote_dns, quote_canonicals,
        k_safe=k_safe, k_risky=k_risky, anchor_vecs=anchor_vecs, client=client,
    )
    for r, cands in zip(pending, cands_per_row):
        r.candidates = cands
        if not r.topk:
            r.topk = [(c.seq, c.cosine) for c in cands if c.tier == "safe"]


# ─── Tier-1 代码直判 + 模型路由 ───────────────────────────────────────────────

def _tier1_assignment(row: SupplierQuoteRow, seq_to_anchor: dict[int, AnchorView]) -> dict | None:
    """canonical 完全一致 + 高 cos + 价格正常 → 直接 align(不进 LLM)。"""
    if not row.topk:
        return None
    if row.unit_price is None or row.unit_price <= 0:
        return None
    best_seq, best_cos = row.topk[0]
    if best_cos < TIER1_COS:
        return None
    anchor = seq_to_anchor.get(best_seq)
    if not anchor:
        return None
    if canonical_match_score(anchor.canonical or {}, row.canonical or {}) != 1.0:
        return None
    return {
        "quote_id": row.quote_id, "anchor_seq": best_seq, "status": "quoted",
        "confidence": round(best_cos, 3),
        "reason": f"Tier1 自动对齐：canonical 完全一致、cos={best_cos:.2f}",
        "_tier1": True,
    }


def _needs_thinking(rows: list[SupplierQuoteRow]) -> bool:
    """Tier-3 触发：Top-K 候选过近 或 高金额(难点裁判)。"""
    for r in rows:
        if len(r.topk) >= 2 and (r.topk[0][1] - r.topk[1][1]) < TIER3_CLOSE_GAP:
            return True
        amount = _row_total(r)
        if amount is not None and amount >= TIER3_HIGH_AMOUNT:
            return True
    return False


def _pick_model(rows: list[SupplierQuoteRow], default_model: str, thinking_model: str | None) -> str:
    if thinking_model and _needs_thinking(rows):
        return thinking_model
    return default_model


# ─── Prompt 构造(中文，供应商视角；用原始表达让 LLM 像人一样看) ───────────────

def build_prompt(
    anchors: list[AnchorView],
    supplier_name: str,
    undecided_rows: list[SupplierQuoteRow],
    tier1_seqs: list[int],
) -> str:
    def _canon_tag(c: dict) -> str:
        parts = [c.get("valve_type"), c.get("dn"), c.get("pn"), c.get("material")]
        s = "/".join(p for p in parts if p)
        return f"[{s}]" if s else ""

    anchor_lines = [
        f"#{int(a.seq)} | {a.name} | {a.spec} {_canon_tag(a.canonical or {})}".rstrip()
        for a in anchors
    ]
    quote_lines = []
    for r in undecided_rows:
        if r.candidates:
            cand_parts = []
            for c in r.candidates:
                marker = "✓" if c.tier == "safe" else "⚠"
                cand_parts.append(f"{marker}#{c.seq}(cos{c.cosine:.2f})")
            cand = "、".join(cand_parts) or "无"
        else:
            cand = "、".join(f"#{seq}(cos{cos:.2f})" for seq, cos in r.topk) or "无"
        hint = ""
        if r.topk and r.topk[0][1] >= 0.70:
            hint = f"  〔高置信，建议直接采用 #{r.topk[0][0]}，除非类型/口径明显不符〕"
        quote_lines.append(
            f"quote_id={r.quote_id} | {r.raw_material or r.material} | "
            f"{r.raw_spec or r.spec} {_canon_tag(r.canonical or {})} | "
            f"单价{r.unit_price} | 数量{r.qty} | 候选锚点: {cand}{hint}"
        )

    tier1_note = (
        f"\n（以下锚点已由系统高置信自动对齐，作为该供应商报价结构的上下文，无需你再处理）：{tier1_seqs}\n"
        if tier1_seqs else ""
    )

    return f"""你是机电材料比价专家。请站在供应商【{supplier_name}】的视角，判断它对采购清单每一项是否报价。

采购清单锚点（共 {len(anchors)} 项）：
{chr(10).join(anchor_lines)}
{tier1_note}
该供应商待判定的报价行（✓=规格相符候选、⚠=存疑候选、选⚠候选时系统自动降为pending）：
{chr(10).join(quote_lines) if quote_lines else "（无）"}

判定规则：
- 逐条报价决定它落到哪个锚点（anchor_seq），或都不落（不输出该条=residue）。
- **anchor_seq 必须从该报价行"候选锚点"列表中选**，不得填写列表以外的值。
- 优先采纳✓高置信候选；仅当 valve_type/DN/PN 明显冲突才改判。
- 允许同一供应商多条报价合并到同一锚点（每条都输出，status 用 "aggregated"）。
- 不确定 → status "pending"（宁缺毋滥，不要为凑可比硬塞 quoted）。
- **不得臆造** quote_id 或 anchor_seq（只能用上面给出的）。
- **不要输出价格**（系统以真实报价为准）；reason 写可核对的证据（名称/规格/口径/cos），不要长篇推理。
- 只返回 JSON，格式：
{{"assignments":[{{"quote_id":123,"anchor_seq":5,"status":"quoted","confidence":0.9,"reason":"..."}}]}}
"""


# ─── LLM 调用 + JSON 解析(镜像 bid_alignment.suggest_alignment) ───────────────

def _parse_llm_json(raw_text: str) -> dict:
    text = (raw_text or "{}").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        text = text[i:j + 1]
    return json.loads(text)


def call_llm(prompt: str, client: Any, model: str, timeout: int = 300) -> tuple[dict, int]:
    """返回 (parsed_json, tokens_used)。解析失败抛异常，由 fill_one_supplier 兜底。"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        timeout=timeout,
    )
    raw = resp.choices[0].message.content or "{}"
    data = _parse_llm_json(raw)
    tokens = 0
    if getattr(resp, "usage", None):
        tokens = getattr(resp.usage, "total_tokens", 0) or 0
    return data, tokens


# ─── 单供应商填表(worker 入口，全程纯数据、无 DB) ─────────────────────────────

def fill_one_supplier(
    rows: list[SupplierQuoteRow],
    anchors: list[AnchorView],
    client: Any,
    supplier_name: str = "",
    anchor_vecs: list[list[float]] | None = None,
    model: str | None = None,
    thinking_model: str | None = None,
    k: int = 3,
    timeout: int = 300,
) -> SupplierFillResult:
    """attach_topk → Tier-1 预判 → build_prompt → call_llm → validate。无 DB 写。"""
    if not rows:
        return SupplierFillResult(supplier_id=0)
    supplier_id = rows[0].supplier_id

    # 1. Wide recall (safe + risky candidates); backfills topk from safe candidates
    attach_wide_candidates(anchors, rows, client=client, anchor_vecs=anchor_vecs)
    seq_to_anchor = {int(a.seq): a for a in anchors}

    # 2. Tier-1 代码直判(不进 LLM，但作为合并 assignment 仍过 validate)
    tier1_assignments: list[dict] = []
    tier1_qids: set[int] = set()
    for r in rows:
        ta = _tier1_assignment(r, seq_to_anchor)
        if ta:
            tier1_assignments.append(ta)
            tier1_qids.add(r.quote_id)

    llm_rows = [r for r in rows if r.quote_id not in tier1_qids]

    # 3. LLM 裁决未决行
    llm_assignments: list[dict] = []
    tokens = 0
    duration_ms = 0
    error = ""
    chosen_model = model or DEFAULT_FILL_MODEL  # keep for repair_pass reference
    if llm_rows:
        chosen_model = model or _pick_model(llm_rows, DEFAULT_FILL_MODEL, thinking_model)
        tier1_seqs = sorted({a["anchor_seq"] for a in tier1_assignments})
        prompt = build_prompt(anchors, supplier_name or str(supplier_id), llm_rows, tier1_seqs)
        t0 = time.time()
        try:
            data, tokens = call_llm(prompt, client, chosen_model, timeout)
            raw_a = data.get("assignments")
            llm_assignments = raw_a if isinstance(raw_a, list) else []
        except Exception as e:  # noqa: BLE001 — 单供应商隔离，不影响他家
            error = f"{type(e).__name__}: {e}"
            log.warning("fill_one_supplier supplier=%s LLM failed: %s", supplier_id, error)
        duration_ms = int((time.time() - t0) * 1000)

    # 4. 合并 + 校验(validate 是唯一裁决与落库依据)
    combined = {"assignments": tier1_assignments + llm_assignments}
    result = validate(combined, anchors, rows)
    # 5. repair_pass for model_invalid_assignment drops
    invalid_qids = [
        d["quote_id"] for d in result.dropped
        if d.get("reason") == "model_invalid_assignment" and d.get("quote_id") is not None
    ]
    if invalid_qids and llm_rows:
        repair_rows = [r for r in llm_rows if r.quote_id in set(invalid_qids)]
        repair_a = repair_pass(
            invalid_qids, repair_rows, anchors, client, chosen_model,
            supplier_name=supplier_name or str(supplier_id), timeout=120,
        )
        if repair_a:
            result.dropped = [d for d in result.dropped if d.get("reason") != "model_invalid_assignment"]
            repair_result = validate({"assignments": repair_a}, anchors, repair_rows)
            result.cells.extend(repair_result.cells)
            repaired_qids = {cell.quote_id for cell in repair_result.cells}
            for qid in invalid_qids:
                if qid not in repaired_qids and qid not in result.residue_quote_ids:
                    result.residue_quote_ids.append(qid)
            result.dropped.extend(d for d in repair_result.dropped if d.get("reason") != "model_invalid_assignment")

    result.tokens_used = tokens
    result.duration_ms = duration_ms
    result.error = error
    return result


# ═══════════════════════════════════════════════════════════════════
#  Wave 2 — anchor-centric fill (逐采购项填表，候选只是参考)
# ═══════════════════════════════════════════════════════════════════

def build_anchor_fill_prompt(
    anchors: list[AnchorView],
    supplier_name: str,
    rows: list[SupplierQuoteRow],
    candidate_map: dict[int, list],   # {anchor_seq: [QuoteCandidate, ...]}
    already_aligned_seqs: set[int] | None = None,
) -> str:
    """Anchor-centric prompt: LLM fills each anchor from the full quote list.

    Candidates are hints only (不是边界). LLM must scan all rows; a missing decision
    requires nearest_quote_candidates evidence. OCR typo rows are flagged with *.
    """
    def _ct(c: dict) -> str:
        parts = [c.get("valve_type"), c.get("dn"), c.get("pn"), c.get("material")]
        s = "/".join(p for p in parts if p)
        return f"[{s}]" if s else ""

    already_aligned = already_aligned_seqs or set()

    # Section 1: anchor table
    anchor_lines: list[str] = []
    for a in anchors:
        if int(a.seq) in already_aligned:
            continue
        qty_s = str(int(a.qty)) if a.qty and a.qty == int(a.qty) else (str(a.qty) if a.qty else "")
        anchor_lines.append(
            f"#{int(a.seq):>3} | {a.name} | {a.spec} {a.pressure} {_ct(a.canonical or {})} | {qty_s} {a.unit}".rstrip()
        )

    # Section 2: full quote rows (保留原始行序，normalized 标*)
    quote_lines: list[str] = []
    for i, r in enumerate(rows):
        nm = r.normalized_material
        raw = r.raw_material or r.material
        mat_display = f"{raw}*→{nm}" if nm else raw
        mt = f" [{r.material_type}]" if r.material_type else ""
        price_s = str(r.unit_price) if r.unit_price is not None else "-"
        qty_s = str(r.qty) if r.qty is not None else "-"
        quote_lines.append(
            f"{i+1:>3} | qid={r.quote_id} | {mat_display}{mt} | "
            f"{r.raw_spec or r.spec} | 单价{price_s} | 数量{qty_s}"
        )

    # Section 3: per-anchor hint summary (compact)
    hint_lines: list[str] = []
    for a in anchors:
        seq = int(a.seq)
        if seq in already_aligned:
            continue
        cands = candidate_map.get(seq) or []
        if not cands:
            hint_lines.append(f"  #{seq}: 无候选")
            continue
        parts = []
        for c in cands[:4]:
            marker = "✓" if c.tier == "safe" else "⚠"
            ocr_flag = "*" if c.has_normalized else ""
            parts.append(f"{marker}qid={c.quote_id}{ocr_flag}(cos{c.cosine:.2f})")
        hint_lines.append(f"  #{seq}: " + " ".join(parts))

    already_note = (
        f"\n（以下 {len(already_aligned)} 个锚点已由第一轮高置信对齐，无需再处理）：{sorted(already_aligned)}\n"
        if already_aligned else ""
    )

    return f"""你是机电材料比价专家。请站在供应商【{supplier_name}】视角，逐一核对下方采购清单每一项，
判断该供应商是否报价，并从完整报价清单中找出对应行。

=== 采购清单（待填{len(anchor_lines)}项）===
#seq | 名称 | 规格/压力/[阀型/DN/PN/材质] | 数量 单位
{chr(10).join(anchor_lines)}
{already_note}
=== 该供应商完整报价清单（共{len(rows)}行，保留原始行序）===
行号 | quote_id | 品名（*=OCR纠错：原文→纠错后）| 规格 | 单价 | 数量
{chr(10).join(quote_lines) if quote_lines else "（无报价）"}

=== 相似度候选参考（✓=规格相符 ⚠=存疑 *=有OCR纠错，仅供参考，不是边界）===
{chr(10).join(hint_lines) if hint_lines else "（无）"}

判定规则：
1. 对每个采购项输出一个 fill，decision = quoted / pending / missing
2. 候选仅参考，**必须扫描全部报价行**，不受候选边界限制
3. 可引用候选列表之外的 quote_id，但要在 evidence 中说明理由
4. OCR 错别字行（标*）：若能对上采购项，在 evidence 说明原始错误，并填写 ocr_correction
5. missing：必须提供 nearest_quote_candidates（Top候选 + why_rejected），无证据不得 missing
6. 不确定 → pending（宁 pending 勿 missing）
7. 多条报价对同一项：decision=quoted，quote_ids 列出所有，evidence 说明聚合逻辑
8. 只返回 JSON，不要解释

输出格式：
{{"fills": [
  {{"anchor_seq": 28, "decision": "quoted", "quote_ids": [13839], "confidence": 0.85,
    "evidence": "报价第5页第27行 橡胶海止回阀 DN50，疑橡胶瓣OCR误识别，DN/数量一致",
    "ocr_correction": {{"from": "橡胶海止回阀", "to": "橡胶瓣止回阀"}},
    "nearest_quote_candidates": []}},
  {{"anchor_seq": 45, "decision": "missing", "quote_ids": [], "confidence": 0.3,
    "evidence": "未找到对应项",
    "nearest_quote_candidates": [{{"quote_id": 13850, "text": "...", "why_rejected": "DN不符"}}]}}
]}}
"""


def validate_anchor_fill(
    raw_llm: dict,
    anchors: list[AnchorView],
    rows: list[SupplierQuoteRow],
) -> SupplierFillResult:
    """Validate anchor-centric LLM output (fills[] keyed by anchor_seq) → SupplierFillResult.

    Differences from validate():
    - Input is fills[] not assignments[] (anchor-keyed not quote-keyed)
    - No (b2) candidate boundary check — LLM may reference any quote_id
    - OCR correction flows into canonical re-check using corrected name
    - missing fills produce no cell (logged in dropped for audit)
    - pending fills produce a pending cell without consuming a quote
    """
    supplier_id = rows[0].supplier_id if rows else 0
    result = SupplierFillResult(supplier_id=supplier_id)

    qid_to_row: dict[int, SupplierQuoteRow] = {r.quote_id: r for r in rows}
    seq_to_anchor: dict[int, AnchorView] = {int(a.seq): a for a in anchors}

    raw_fills = raw_llm.get("fills") if isinstance(raw_llm, dict) else None
    if not isinstance(raw_fills, list):
        raw_fills = []

    consumed: set[int] = set()     # quote_ids consumed by quoted/aggregated fills (exclusive)
    referenced: set[int] = set()   # ALL quote_ids referenced by any fill (incl. pending/excluded)
    seen_seqs: set[int] = set()    # dedupe anchor_seq

    for f in raw_fills:
        if not isinstance(f, dict):
            continue
        seq = _coerce_int(f.get("anchor_seq"))
        if seq is None or seq not in seq_to_anchor:
            result.dropped.append({"anchor_seq": seq, "reason": "unknown_anchor_seq"})
            continue
        if seq in seen_seqs:
            result.dropped.append({"anchor_seq": seq, "reason": "duplicate_anchor_seq"})
            continue
        seen_seqs.add(seq)

        anchor = seq_to_anchor[seq]
        decision = str(f.get("decision") or "").strip().lower()
        confidence = _coerce_float(f.get("confidence")) or 0.0
        evidence = str(f.get("evidence") or "").strip()
        raw_qids = f.get("quote_ids") or []
        nearest = f.get("nearest_quote_candidates") or []

        # P1: missing requires nearest_quote_candidates evidence; bare missing → pending
        flags: list[str] = []
        if decision == "missing":
            if nearest:
                # Sufficient evidence — treat as reliable missing, no cell produced
                result.dropped.append({
                    "anchor_seq": seq,
                    "reason": "llm_missing",
                    "evidence": evidence,
                    "nearest_quote_candidates": nearest,
                })
                continue
            else:
                # No evidence — downgrade to pending for human review
                result.dropped.append({
                    "anchor_seq": seq,
                    "reason": "invalid_missing_no_evidence",
                    "evidence": evidence,
                })
                decision = "pending"
                flags = ["missing_without_evidence"]
                # Fall through: valid_qids will be [] → pending placeholder cell

        if decision not in ("quoted", "pending", "excluded", "aggregated"):
            decision = "pending"

        # Resolve quote_ids — filter invalid
        valid_qids: list[int] = []
        for qid_raw in raw_qids:
            qid = _coerce_int(qid_raw)
            if qid is None or qid not in qid_to_row:
                result.dropped.append({"anchor_seq": seq, "quote_id": qid, "reason": "unknown_quote_id"})
                continue
            valid_qids.append(qid)

        if not valid_qids:
            # No valid rows → downgrade to pending
            decision = "pending"

        # Track all referenced qids to exclude from residue (P1)
        referenced.update(valid_qids)

        # OCR correction re-check: if LLM provided ocr_correction, verify corrected canonical
        ocr_corr = f.get("ocr_correction") or {}
        corrected_name = str(ocr_corr.get("to") or "").strip() if ocr_corr else ""
        corr_from = str(ocr_corr.get("from") or "").strip() if ocr_corr else ""

        # canonical gate: check against each referenced row
        if decision in ("quoted", "aggregated") and valid_qids:
            for qid in valid_qids:
                row = qid_to_row[qid]
                # Use corrected canonical if OCR correction was applied
                if corrected_name:
                    corrected_canon = extract_valve_canonical(corrected_name, row.raw_spec or row.spec)
                    row_canon = corrected_canon if corrected_canon.get("valve_type") else row.canonical
                else:
                    row_canon = row.canonical
                cscore = canonical_match_score(anchor.canonical or {}, row_canon or {})
                if cscore == 0.0:
                    flags.append("canonical_conflict")
                    decision = "pending"
                    break

        # Fresh valve-type conflict gate (g2): re-extract from raw quote text.
        # Catches stale DB canonical (valve_type=None) that belongs to a different product class.
        # Exception: a plausible OCR correction (from→to within the same type family) may bypass
        # this gate; implausible corrections (流量测试→闸阀, 真空破坏器→减压阀组) still fail.
        if decision in ("quoted", "aggregated") and valid_qids:
            _anchor_vt = (anchor.canonical or {}).get("valve_type")
            if _anchor_vt:
                _ocr_bypass = False
                for qid in valid_qids:
                    row = qid_to_row[qid]
                    _qt = " ".join(filter(None, [
                        row.normalized_material, row.raw_material, row.material
                    ]))
                    # If OCR correction provided, check type-family plausibility first.
                    # Use explicit corr_from if given, else fall back to raw quote text.
                    if corrected_name:
                        _effective_from = corr_from or _qt
                        if _effective_from and _ocr_correction_type_plausible(
                            _effective_from, corrected_name, _anchor_vt
                        ):
                            _ocr_bypass = True
                            continue  # Plausible OCR fix — skip raw conflict check for this qid
                        # Implausible correction: fall through to raw text conflict check
                    if _qt:
                        _fresh = extract_valve_canonical(_qt, row.raw_spec or row.spec)
                        _q_vt = _fresh.get("valve_type")
                        if _q_vt and not valve_type_compatible(_anchor_vt, _q_vt):
                            flags.append(f"valve_type_conflict:{_q_vt}")
                            decision = "pending"
                            result.dropped.append({
                                "anchor_seq": seq, "quote_id": qid,
                                "reason": "valve_type_conflict",
                                "anchor_vt": _anchor_vt, "quote_vt": _q_vt,
                                "quote_text": _qt[:80],
                            })
                            break
                if _ocr_bypass and "ocr_corrected_verified" not in flags:
                    flags.append("ocr_corrected_verified")

        if corrected_name:
            flags.append("ocr_corrected")

        # Duplicate consumption guard (only for quoted/aggregated — pending can share)
        if decision in ("quoted", "aggregated"):
            already_consumed = [q for q in valid_qids if q in consumed]
            fresh = [q for q in valid_qids if q not in consumed]
            if already_consumed:
                flags.append(f"dup_qids:{already_consumed}")
            if not fresh:
                decision = "pending"
                valid_qids = already_consumed[:1] if already_consumed else []
            else:
                valid_qids = fresh
                consumed.update(fresh)

        # Build cell(s)
        action = _action_for_status(decision)
        if len(valid_qids) >= 2 and decision in ("quoted", "aggregated"):
            # aggregated
            members = [qid_to_row[q] for q in valid_qids]
            agg_total = sum(t for t in (_row_total(m) for m in members) if t is not None)
            agg_qty = sum(q for q in (m.qty for m in members) if q is not None)
            rep = max(members, key=lambda m: _row_total(m) or 0.0)
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id,
                action="align", status="aggregated",
                quote_id=rep.quote_id,
                unit_price=rep.unit_price, qty=rep.qty,
                total_price=_row_total(rep),
                agg_total=round(agg_total, 4), agg_qty=agg_qty or None,
                aggregated_quote_ids=[m.quote_id for m in members],
                confidence=confidence, reason=evidence, flags=flags,
            ))
        elif valid_qids:
            row = qid_to_row[valid_qids[0]]
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id,
                action=action, status=decision,
                quote_id=row.quote_id,
                unit_price=row.unit_price, qty=row.qty,
                total_price=_row_total(row),
                confidence=confidence, reason=evidence, flags=flags,
            ))
        else:
            # pending with no quote ref — attach a placeholder
            result.cells.append(FillCell(
                anchor_seq=seq, supplier_id=supplier_id,
                action="pending", status="pending",
                quote_id=0,
                confidence=confidence, reason=evidence, flags=flags,
            ))

    # residue: rows NOT referenced by any fill (quoted, pending, aggregated, excluded)
    # A row in a pending cell is already assigned for human review and must not appear twice.
    for row in rows:
        if row.quote_id not in referenced:
            result.residue_quote_ids.append(row.quote_id)
            if row.best_cos() >= RESIDUE_HIGH_COS:
                result.residue_high_cos += 1

    if result.dropped:
        log.debug(
            "validate_anchor_fill supplier=%s dropped=%d",
            supplier_id, len(result.dropped),
        )
    return result


def fill_one_supplier_anchor_centric(
    rows: list[SupplierQuoteRow],
    anchors: list[AnchorView],
    client: Any,
    supplier_name: str = "",
    anchor_vecs: list[list[float]] | None = None,
    model: str | None = None,
    timeout: int = 300,
    k_hints: int = 5,
    already_aligned_seqs: set[int] | None = None,
) -> SupplierFillResult:
    """Anchor-centric fill worker (Wave 2).

    Uses ALL rows (not just residue) and per-anchor candidate hints. The LLM receives
    the complete supplier quote list + anchor list + hints, and fills each anchor item
    from scratch — like a procurement officer checking line by line.

    already_aligned_seqs: anchor seqs already covered by first pass; excluded from prompt.
    Returns partial SupplierFillResult with new cells (merged by caller).
    """
    if not rows:
        return SupplierFillResult(supplier_id=0)
    supplier_id = rows[0].supplier_id

    from apps.api.services.anchor_match import attach_nearest_hints

    # Per-anchor Top-K hints; pure cosine, no hard blocks
    candidate_map = attach_nearest_hints(
        anchors, rows, client=client, k=k_hints, anchor_vecs=anchor_vecs,
    )

    prompt = build_anchor_fill_prompt(
        anchors=anchors,
        supplier_name=supplier_name or str(supplier_id),
        rows=rows,
        candidate_map=candidate_map,
        already_aligned_seqs=already_aligned_seqs,
    )

    chosen_model = model or DEFAULT_FILL_MODEL
    t0 = time.time()
    tokens = 0
    error = ""
    raw_llm: dict = {}

    try:
        raw_llm, tokens = call_llm(prompt, client, chosen_model, timeout)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        log.warning("fill_one_supplier_anchor_centric supplier=%s LLM failed: %s",
                    supplier_id, error)

    duration_ms = int((time.time() - t0) * 1000)
    result = validate_anchor_fill(raw_llm, anchors, rows)
    result.tokens_used = tokens
    result.duration_ms = duration_ms
    result.error = error
    return result


# ─── Repair pass ────────────────────────────────────────────────────────────

def repair_pass(
    invalid_qids: list[int],
    repair_rows: list[SupplierQuoteRow],
    anchors: list[AnchorView],
    client: Any,
    model: str,
    supplier_name: str = "",
    timeout: int = 120,
) -> list[dict]:
    """One-shot re-prompt for model_invalid_assignment rows with top-5 candidates.

    Returns assignments list (may be empty). Fail → return [] (caller handles residue).
    """
    if not repair_rows:
        return []

    import copy
    limited: list[SupplierQuoteRow] = []
    for r in repair_rows:
        r2 = copy.copy(r)
        top5 = sorted(r.candidates, key=lambda c: c.combined, reverse=True)[:5]
        r2.candidates = top5
        r2.topk = [(c.seq, c.cosine) for c in top5 if c.tier == "safe"]
        limited.append(r2)

    prompt = build_prompt(anchors, supplier_name, limited, [])
    prompt += (
        "\n（以上报价行在上一轮填表时输出了候选列表以外的 anchor_seq，请重新从候选列表中选择，"
        "或不输出该条。）"
    )
    try:
        data, _ = call_llm(prompt, client, model, timeout)
        return data.get("assignments") or []
    except Exception as e:  # noqa: BLE001
        log.warning("repair_pass failed: %s", e)
        return []


# ─── Anchor-centric second pass ──────────────────────────────────────────────

def _build_anchor_centric_prompt(
    gap_anchors: list[AnchorView],
    supplier_name: str,
    rows: list[SupplierQuoteRow],
) -> str:
    def _ct(c: dict) -> str:
        parts = [c.get("valve_type"), c.get("dn"), c.get("pn"), c.get("material")]
        s = "/".join(p for p in parts if p)
        return f"[{s}]" if s else ""

    anchor_lines = [
        f"#{int(a.seq)} | {a.name} | {a.spec} {_ct(a.canonical or {})}".rstrip()
        for a in gap_anchors
    ]
    quote_lines = [
        f"quote_id={r.quote_id} | {r.raw_material or r.material} | "
        f"{r.raw_spec or r.spec} {_ct(r.canonical or {})} | 单价{r.unit_price}"
        for r in rows
    ]
    return f"""你是机电材料比价专家。以下是供应商【{supplier_name}】中**尚未覆盖的缺口采购项**，
以及该供应商尚未归档的报价行。请判断哪些报价行能对应这些缺口采购项。

缺口采购项（{len(gap_anchors)} 个）：
{chr(10).join(anchor_lines)}

该供应商未归档报价行：
{chr(10).join(quote_lines) if quote_lines else "（无）"}

判定规则：
- 只输出上面缺口采购项的 anchor_seq，不要输出其他锚点。
- 不确定 → status "pending"（宁缺毋滥）。
- **不得臆造** quote_id 或 anchor_seq。
- 只返回 JSON，格式：{{"assignments":[{{"quote_id":123,"anchor_seq":5,"status":"quoted","confidence":0.8,"reason":"..."}}]}}
"""


def _validate_anchor_centric(
    assignments: list[dict],
    valid_gap_seqs: set[int],
    rows: list[SupplierQuoteRow],
) -> list[dict]:
    """Minimal validation for anchor-centric pass: seq must be in gap set, no dup qid."""
    qid_set = {r.quote_id for r in rows}
    consumed: set[int] = set()
    valid: list[dict] = []
    for a in assignments:
        if not isinstance(a, dict):
            continue
        qid = _coerce_int(a.get("quote_id"))
        seq = _coerce_int(a.get("anchor_seq"))
        if qid is None or qid not in qid_set:
            continue
        if seq is None or seq not in valid_gap_seqs:
            continue
        if qid in consumed:
            continue
        consumed.add(qid)
        valid.append(a)
    return valid


def anchor_centric_pass(
    gap_anchor_seqs: list[int],
    residue_rows_by_sid: dict[int, list[SupplierQuoteRow]],
    anchors: list[AnchorView],
    first_pass_cells_by_sid: dict[int, list[FillCell]],
    client: Any,
    model: str = DEFAULT_FILL_MODEL,
    supplier_names: dict[int, str] | None = None,
    timeout: int = 300,
    max_tasks: int = 60,
) -> dict[int, SupplierFillResult]:
    """DEPRECATED — use fill_one_supplier_anchor_centric() called per-supplier in analysis.py.

    This function:
    - only sees residue rows (misses first-pass wrong-matches)
    - uses old assignments[] schema instead of fills[]
    - does not support anchor_vecs or already_aligned_seqs
    - does not include SUSPECT_SEQS override logic

    Kept for reference; not called by any production path since Wave 2.
    """
    supplier_names = supplier_names or {}
    seq_to_anchor = {int(a.seq): a for a in anchors}
    gap_anchors_map = {seq: seq_to_anchor[seq] for seq in gap_anchor_seqs if seq in seq_to_anchor}
    if not gap_anchors_map:
        return {}

    tasks: list[tuple[int, list[SupplierQuoteRow], list[int]]] = []
    for sid, residue_rows in residue_rows_by_sid.items():
        if not residue_rows:
            continue
        filled_seqs = {
            cell.anchor_seq
            for cell in first_pass_cells_by_sid.get(sid, [])
            if cell.status in ("quoted", "aggregated")
        }
        remaining_gaps = [seq for seq in gap_anchor_seqs if seq not in filled_seqs]
        if not remaining_gaps:
            continue
        tasks.append((sid, residue_rows, remaining_gaps))
        if len(tasks) >= max_tasks:
            break

    results: dict[int, SupplierFillResult] = {}
    for sid, rows, gap_seqs in tasks:
        gap_anchor_views = [gap_anchors_map[seq] for seq in gap_seqs if seq in gap_anchors_map]
        name = supplier_names.get(sid, str(sid))
        prompt = _build_anchor_centric_prompt(gap_anchor_views, name, rows)
        try:
            data, tokens = call_llm(prompt, client, model, timeout)
            raw_a = data.get("assignments") or []
            valid_a = _validate_anchor_centric(raw_a, set(gap_seqs), rows)
            partial_result = validate({"assignments": valid_a}, list(gap_anchors_map.values()), rows)
            partial_result.tokens_used = tokens
            results[sid] = partial_result
        except Exception as e:  # noqa: BLE001
            log.warning("anchor_centric_pass sid=%s failed: %s", sid, e)
            results[sid] = SupplierFillResult(supplier_id=sid, error=str(e))

    return results
