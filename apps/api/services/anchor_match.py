"""锚点匹配服务(docs/design/05 §9 第3步)。

把招标清单锚点行 + 供应商报价做嵌入语义匹配,落成 BidAlignmentGroup(锚点=组,
报价=组内 item),现有 bid-matrix 自动渲染成"锚点行 × 供应商"比价矩阵。

分层(本版到 Tier2):
  Tier1 嵌入召回:每条报价找余弦最近、DN 一致的锚点
  Tier2 canonical 硬过滤:valve_type/DN/PN 冲突 → hard block 0.0
  Tier3 闸②LLM复核 + 缓存:暂缓(见设计文档 §9 决策 2026-06-08)

Combined score: cos × (1.0 + 0.3 × c_score) 提升同类型匹配排序。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

from openai import OpenAI
from sqlalchemy.orm import Session

from apps.api.core.config import get_settings
from apps.api.models import Material, Quote
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.models.supplier import Supplier
from apps.api.services.canonical import (
    canonical_match_score, extract_valve_canonical, valve_type_compatible,
)
from apps.api.services.tender_list import TenderAnchor, parse_tender_xlsx


@dataclass
class _BQLProxy:
    """Thin wrapper so BidQuoteLine rows slot into the Quote-indexed match lists."""
    id: int                          # bql.id — stored as bid_quote_line_id in BidAlignmentItem
    supplier_id: int                 # group key: sub.id (submission_id)
    submission_id: int = 0           # sub.id — written to BidAlignmentItem.submission_id
    actual_supplier_id: int | None = None  # sub.supplier_id (soft-ref, can be None)
    total_price: float | None = None
    quantity: float | None = None    # = bql.qty
    brand: str = ""
    canonical: dict | None = None    # from bql.canonical; used for canonical matching
    is_bql: bool = True
    document_row_index: int | None = None  # 全局文档行序(1..N)，顺序直连优先用它对齐


@dataclass
class _BQLMatProxy:
    """Material-compatible proxy for BQL rows (no DB-backed extended_attrs)."""
    standard_name: str
    spec: str
    unit: str
    extended_attrs: dict | None = None

# 余弦低于此视为无可信锚点(与测量脚本一致)
SIM_THRESHOLD = 0.50
# 低于此标记为低置信、建议复核(写入 reason,前端可高亮)
LOW_CONF = 0.70
EMBED_MODEL = "text-embedding-v3"
_EMBED_BATCH = 10


@dataclass
class MatchSummary:
    anchors_total: int
    anchors_covered: int          # 至少 1 家报价的锚点数
    comparable_2plus: int         # ≥2 家可比的锚点数
    three_way: int                # 三家齐全的锚点数
    matched_quotes: int
    total_quotes: int
    low_conf: int                 # 低置信匹配数(建议复核)
    residue: int                  # 未匹配报价数

    def as_dict(self) -> dict:
        return {
            "anchors_total": self.anchors_total,
            "anchors_covered": self.anchors_covered,
            "comparable_2plus": self.comparable_2plus,
            "three_way": self.three_way,
            "matched_quotes": self.matched_quotes,
            "total_quotes": self.total_quotes,
            "low_conf": self.low_conf,
            "residue": self.residue,
        }


def _dn_of(s: str) -> int | None:
    m = re.search(r"DN\s*0*(\d+)", s or "", re.I)
    return int(m.group(1)) if m else None


def _embed_client() -> OpenAI:
    s = get_settings()
    key = s.DASHSCOPE_API_KEY
    if not key and getattr(s, "DASHSCOPE_API_KEYS", ""):
        key = s.DASHSCOPE_API_KEYS.split(",")[0].strip()
    return OpenAI(api_key=key, base_url=s.DASHSCOPE_BASE_URL)


def _embed(client: OpenAI, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        r = client.embeddings.create(model=EMBED_MODEL, input=texts[i:i + _EMBED_BATCH])
        out.extend(d.embedding for d in r.data)
    return out


def _cosine_matrix(Q: list[list[float]], A: list[list[float]]) -> list[list[float]]:
    """纯 Python 余弦(规模 ~200×90,无需 numpy 依赖)。"""
    def norm(v):
        s = sum(x * x for x in v) ** 0.5
        return [x / s for x in v] if s else v
    Qn = [norm(q) for q in Q]
    An = [norm(a) for a in A]
    return [[sum(qi * ai for qi, ai in zip(q, a)) for a in An] for q in Qn]


def embed_anchor_vecs(anchors: list[TenderAnchor], client: OpenAI | None = None) -> list[list[float]]:
    """Embed the anchor axis once so callers (e.g. N supplier-fill workers) can
    reuse the vectors instead of re-embedding 90 anchors per supplier."""
    if not anchors:
        return []
    client = client or _embed_client()
    a_text = [f"{a.name} {a.spec} {a.pressure} {a.material_text()}".strip() for a in anchors]
    return _embed(client, a_text)


def _anchor_dns(anchors: list[TenderAnchor]) -> list[int | None]:
    return [_dn_of(a.spec) or _dn_of(a.name) for a in anchors]


def _score_anchors_for_quote(
    sims_row: list[float],
    anchors: list[TenderAnchor],
    a_dn: list[int | None],
    q_dn: int | None,
    q_canon: dict,
) -> list[tuple[int, float, float, float]]:
    """Score every candidate anchor for one quote.

    Returns [(anchor_idx, cos, c_score, combined), ...] for anchors that pass the
    SIM_THRESHOLD + DN hard-filter + canonical hard-block (0.0 → excluded). Shared
    by match_anchors (argmax) and match_anchors_topk (top-K). c_score is 0.5
    (neutral) when neither side carries canonical info.
    """
    scored: list[tuple[int, float, float, float]] = []
    for ai in range(len(anchors)):
        cos = sims_row[ai]
        if cos < SIM_THRESHOLD:
            continue
        if q_dn is not None and a_dn[ai] is not None and q_dn != a_dn[ai]:
            continue
        a_canon = getattr(anchors[ai], "canonical", {}) or {}
        if q_canon or a_canon:
            c_score = canonical_match_score(a_canon, q_canon)
            if c_score == 0.0:
                continue  # hard block: valve_type / DN / PN conflict
            combined = cos * (1.0 + 0.3 * c_score)
        else:
            c_score = 0.5
            combined = cos
        scored.append((ai, cos, c_score, combined))
    return scored


def match_anchors(
    anchors: list[TenderAnchor],
    quotes: list[Quote],
    quote_texts: list[str],
    quote_dns: list[int | None],
    client: OpenAI | None = None,
    quote_canonicals: list[dict] | None = None,
) -> list[tuple[int, int, float]]:
    """返回 [(quote_idx, anchor_idx, cosine), ...](未匹配的不在列表里)。

    quote_canonicals: per-quote canonical dicts for hard-filter + combined scoring.
    If None, falls back to v2.3 cosine-only behavior (backward compatible).
    """
    if not anchors or not quotes:
        return []
    client = client or _embed_client()
    a_dn = _anchor_dns(anchors)
    A = embed_anchor_vecs(anchors, client)
    Q = _embed(client, quote_texts)
    sims = _cosine_matrix(Q, A)

    result: list[tuple[int, int, float]] = []
    for qi in range(len(quotes)):
        q_canon = (quote_canonicals[qi] if quote_canonicals else None) or {}
        scored = _score_anchors_for_quote(sims[qi], anchors, a_dn, quote_dns[qi], q_canon)
        if not scored:
            continue
        best_ai, best_cos, _c, _comb = max(scored, key=lambda x: x[3])
        result.append((qi, best_ai, best_cos))

    return result


def match_anchors_topk(
    anchors: list[TenderAnchor],
    quotes: list[Quote],
    quote_texts: list[str],
    quote_dns: list[int | None],
    client: OpenAI | None = None,
    quote_canonicals: list[dict] | None = None,
    k: int = 3,
    anchor_vecs: list[list[float]] | None = None,
) -> list[list[tuple[int, float, float]]]:
    """Per-quote Top-K anchor candidates for the LLM supplier-fill agent.

    Returns result[qi] = [(anchor_idx, cosine, c_score), ...] — up to K candidates
    ordered by combined score (cos × (1 + 0.3·c_score)) descending. Candidates that
    violate the DN hard-filter or canonical hard-block (0.0) are excluded, so the
    LLM never sees a hint that contradicts a hard rule. Empty list when no candidate.

    anchor_vecs: pre-embedded anchor vectors (from embed_anchor_vecs) to avoid
    re-embedding the 90 anchors for every supplier.
    """
    if not anchors or not quotes:
        return [[] for _ in quotes]
    client = client or _embed_client()
    a_dn = _anchor_dns(anchors)
    A = anchor_vecs if anchor_vecs is not None else embed_anchor_vecs(anchors, client)
    Q = _embed(client, quote_texts)
    sims = _cosine_matrix(Q, A)

    out: list[list[tuple[int, float, float]]] = []
    for qi in range(len(quotes)):
        q_canon = (quote_canonicals[qi] if quote_canonicals else None) or {}
        scored = _score_anchors_for_quote(sims[qi], anchors, a_dn, quote_dns[qi], q_canon)
        scored.sort(key=lambda x: x[3], reverse=True)
        out.append([(ai, cos, c_score) for ai, cos, c_score, _comb in scored[:k]])
    return out


@dataclass
class AnchorCandidate:
    """One candidate anchor for a quote row, with safety tier label.

    tier values:
      "safe"                  — passes all hard-filters (cos ≥ threshold, DN ok, canonical ok)
      "risky_dn_mismatch"     — DN conflict between quote and anchor
      "risky_canonical_conflict" — canonical_match_score == 0.0 (valve_type/DN/PN clash)
      "risky_low_similarity"  — cos in [min_cos_risky, SIM_THRESHOLD)

    Risky candidates are shown to LLM but validator must downgrade to pending if selected.
    """
    seq: int
    anchor_idx: int
    cosine: float
    c_score: float   # canonical_match_score (0.5 = no canonical info)
    combined: float  # cos × (1 + 0.3 × c_score)
    tier: str


# Minimum cosine for risky candidates — below this is too noisy to show LLM
_MIN_COS_RISKY = 0.35


def match_anchors_wide(
    anchors,
    rows,
    quote_texts: list[str],
    quote_dns: list[int | None],
    quote_canonicals: list[dict],
    k_safe: int = 5,
    k_risky: int = 5,
    anchor_vecs: list[list[float]] | None = None,
    client: "OpenAI | None" = None,
) -> list[list[AnchorCandidate]]:
    """Wide Top-(k_safe + k_risky) recall with safe/risky tier classification.

    Unlike match_anchors_topk which hard-blocks risky items, this function:
    - safe: cos ≥ SIM_THRESHOLD AND no DN conflict AND canonical_score > 0 (or no canonical)
    - risky_dn_mismatch: cos ≥ _MIN_COS_RISKY but DN conflict
    - risky_canonical_conflict: cos ≥ _MIN_COS_RISKY but canonical_score == 0.0
    - risky_low_similarity: _MIN_COS_RISKY ≤ cos < SIM_THRESHOLD

    Returns per-row list: safe candidates first (sorted by combined), then risky (sorted by cosine).
    Risky candidates must only become pending in the LLM stage, never quoted.
    """
    if not anchors or not rows:
        return [[] for _ in rows]
    client = client or _embed_client()
    a_dn = _anchor_dns(anchors)
    A = anchor_vecs if anchor_vecs is not None else embed_anchor_vecs(anchors, client)
    Q = _embed(client, quote_texts)
    sims = _cosine_matrix(Q, A)

    idx_to_seq = {i: int(getattr(a, "seq", i)) for i, a in enumerate(anchors)}
    result: list[list[AnchorCandidate]] = []

    for qi in range(len(rows)):
        q_dn = quote_dns[qi] if qi < len(quote_dns) else None
        q_canon = quote_canonicals[qi] if qi < len(quote_canonicals) else {}
        sims_row = sims[qi]

        safe: list[AnchorCandidate] = []
        risky: list[AnchorCandidate] = []

        for ai in range(len(anchors)):
            cos = sims_row[ai]
            if cos < _MIN_COS_RISKY:
                continue  # too noisy even for risky list

            seq = idx_to_seq[ai]
            a_canon = getattr(anchors[ai], "canonical", {}) or {}

            dn_conflict = (q_dn is not None and a_dn[ai] is not None and q_dn != a_dn[ai])

            if q_canon or a_canon:
                c_score = canonical_match_score(a_canon, q_canon)
                canonical_conflict = (c_score == 0.0)
            else:
                c_score = 0.5
                canonical_conflict = False

            combined = cos * (1.0 + 0.3 * c_score)
            low_sim = cos < SIM_THRESHOLD

            if dn_conflict:
                tier = "risky_dn_mismatch"
            elif canonical_conflict:
                tier = "risky_canonical_conflict"
            elif low_sim:
                tier = "risky_low_similarity"
            else:
                tier = "safe"

            cand = AnchorCandidate(
                seq=seq, anchor_idx=ai, cosine=cos, c_score=c_score, combined=combined, tier=tier,
            )
            if tier == "safe":
                safe.append(cand)
            else:
                risky.append(cand)

        safe.sort(key=lambda c: c.combined, reverse=True)
        risky.sort(key=lambda c: c.cosine, reverse=True)
        result.append(safe[:k_safe] + risky[:k_risky])

    return result


@dataclass
class QuoteCandidate:
    """One candidate quote row for an anchor (anchor-centric direction).

    tier values:
      "safe"                     — cos ≥ SIM_THRESHOLD, no canonical conflict
      "risky_ocr_typo"           — row has normalized_material (OCR correction present)
      "risky_canonical_conflict" — canonical_match_score == 0.0
      "risky_low_similarity"     — cos in [_MIN_COS_RISKY, SIM_THRESHOLD)
      "risky_material_missing"   — no canonical info on either side (can't confirm safe)

    All tiers are shown to LLM; risky tiers result in pending if selected without
    strong evidence. There is no hard-block (that's the key difference from AnchorCandidate).
    """
    quote_id: int
    quote_idx: int
    cosine: float
    c_score: float
    combined: float
    tier: str
    has_normalized: bool = False


def attach_nearest_hints(
    anchors: list,
    rows: list,
    client: "OpenAI | None" = None,
    k: int = 5,
    anchor_vecs: list[list[float]] | None = None,
) -> dict[int, list[QuoteCandidate]]:
    """Per-anchor Top-K quote candidates — anchor-centric direction (reversed vs match_anchors_wide).

    Pure cosine initial sort; NO canonical/DN hard-filter (risky rows stay visible so
    the LLM can apply OCR error correction). Tier labels signal confidence but never block.

    anchors: list of AnchorView (from supplier_fill_llm) or TenderAnchor.
    rows: list of SupplierQuoteRow — ALL rows for this supplier, not just residue.
    Returns {anchor_seq: [QuoteCandidate, ...]} sorted by combined score desc.
    """
    if not anchors or not rows:
        return {}

    client = client or _embed_client()

    # Embed quote rows — use normalized_material when available (corrected text = better embedding)
    quote_texts: list[str] = []
    for r in rows:
        nm = str(getattr(r, "normalized_material", "") or "")
        mat = nm or str(getattr(r, "raw_material", "") or getattr(r, "material", "") or "")
        spec = str(getattr(r, "raw_spec", "") or getattr(r, "spec", "") or "")
        quote_texts.append(f"{mat} {spec}".strip() or str(mat))

    # P0 guard: if caller passed anchor_vecs for a different (larger) anchor set, re-embed.
    # This happens when analysis.py pre-computes full-90-anchor vectors then passes them
    # to an AC pass that only uses a gap subset.
    if anchor_vecs is not None and len(anchor_vecs) != len(anchors):
        log.debug(
            "attach_nearest_hints: anchor_vecs length %d != anchors length %d — re-embedding subset",
            len(anchor_vecs), len(anchors),
        )
        anchor_vecs = None

    A = anchor_vecs if anchor_vecs is not None else embed_anchor_vecs(anchors, client)
    Q = _embed(client, quote_texts)
    # sims[qi][ai] = cosine(quote_qi, anchor_ai) — symmetric, same values as A×Q direction
    sims = _cosine_matrix(Q, A)

    idx_to_seq = {i: int(getattr(a, "seq", i)) for i, a in enumerate(anchors)}
    result: dict[int, list[QuoteCandidate]] = {}

    for ai, anchor in enumerate(anchors):
        seq = idx_to_seq[ai]
        a_canon = getattr(anchor, "canonical", {}) or {}
        candidates: list[QuoteCandidate] = []

        for qi, row in enumerate(rows):
            cos = sims[qi][ai]
            if cos < _MIN_COS_RISKY:
                continue

            r_canon = getattr(row, "canonical", {}) or {}
            has_norm = bool(getattr(row, "normalized_material", ""))

            if r_canon or a_canon:
                c_score = canonical_match_score(a_canon, r_canon)
            else:
                c_score = 0.5

            combined = cos * (1.0 + 0.3 * c_score)

            # Tier — no hard blocks; all candidates surfaced to LLM
            if has_norm:
                # OCR typo row: show always (LLM error-correction is the point)
                tier = "risky_ocr_typo"
            elif c_score == 0.0:
                tier = "risky_canonical_conflict"
            elif cos < SIM_THRESHOLD:
                tier = "risky_low_similarity"
            elif not r_canon and not a_canon:
                tier = "risky_material_missing"
            else:
                tier = "safe"

            candidates.append(QuoteCandidate(
                quote_id=getattr(row, "quote_id", 0),
                quote_idx=qi,
                cosine=round(cos, 4),
                c_score=round(c_score, 4),
                combined=round(combined, 4),
                tier=tier,
                has_normalized=has_norm,
            ))

        candidates.sort(key=lambda c: c.combined, reverse=True)
        result[seq] = candidates[:k]

    return result


# 顺序直连门禁阈值（整表层：决定该供应商是否走顺序直连）
_SEQ_DN_COVERAGE = 0.90      # 双方都识别出 DN 的位置占比下限（防稀疏DN蒙混整表通过）
_SEQ_DN_CONSISTENCY = 0.95   # 已识别DN的同位置一致率（确认整体按位置对齐，非shuffle）
_SEQ_FAM_CONSISTENCY = 0.90  # 同位置大类族一致率（整表防整体/局部交换）
_SEQ_QTY_TOL = 0.001         # 数量比较容差

# 归一化大类族：稳健于名称变体（止回阀内部橡胶瓣/缓闭式/弹簧式 + 倒流防止器 同族），
# 但 蝶阀≠闸阀≠球阀 严格区分——用于逐行 + 整表防同DN串位。关键字按特异性排序，先到先得。
_COARSE_FAMILY_KEYWORDS: list[tuple[str, str]] = [
    ("蝶阀", "butterfly"), ("闸阀", "gate"), ("球阀", "ball"),
    ("截止阀", "globe"), ("节流", "throttle"),
    ("止回", "check"), ("逆止", "check"), ("倒流", "check"), ("防止器", "check"),
    ("减压", "reducing"), ("安全阀", "safety"), ("调节阀", "control"),
    ("平衡阀", "balance"), ("排气", "air"), ("排泥", "drain"), ("泄压", "relief"),
    ("过滤", "filter"), ("滤器", "filter"), ("滤网", "filter"), ("除污", "filter"),
    ("水表", "meter"), ("流量计", "meter"), ("压力表", "gauge"), ("温度计", "gauge"),
]


def _coarse_family(name: str | None) -> str | None:
    """名称 → 归一化大类族（无法判定返回 None，不参与冲突判定）。"""
    n = name or ""
    for kw, fam in _COARSE_FAMILY_KEYWORDS:
        if kw in n:
            return fam
    return None


def _qty_eq(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= _SEQ_QTY_TOL
    except (TypeError, ValueError):
        return False


def _doc_order(qis: list[int], doc_index: dict | None) -> tuple[list[int], bool]:
    """决定该 submission 的文档顺序。返回 (ordered_qis, reject)。

    三态（不把"损坏的业务序号"静默替换成数据库插入顺序）：
      - 全部行有 document_row_index 且完整/唯一/连续 → 用它排序，reject=False；
      - 该 submission 全部行都无 document_row_index（历史数据）→ 回退载入顺序(=submission_id,id)，
        reject=False（legacy_order_fallback，已 log）；
      - 部分有但残缺/重复/不连续 → reject=True：业务序号已损坏，禁止顺序直连，安全回退语义。
    """
    vals = [(doc_index or {}).get(qi) for qi in qis]
    present = [v for v in vals if v is not None]
    if not present:
        log.info("sequential: no document_row_index (legacy_order_fallback) — 用入库(id)顺序")
        return qis, False                       # 历史数据：ID 顺序兼容
    if len(present) != len(qis):
        return qis, True                        # 部分缺失 → 损坏 → 拒绝
    if len(set(present)) != len(present):
        return qis, True                        # 重复 → 拒绝
    lo = min(present)
    if sorted(present) != list(range(lo, lo + len(present))):
        return qis, True                        # 不连续 → 拒绝
    return sorted(qis, key=lambda qi: doc_index[qi]), False


def _sequential_matches(
    anchors: list,
    quotes: list,
    materials: list,
    quote_dns: list,
    quote_canonicals: list,
    doc_index: dict | None = None,
) -> tuple[list[tuple[int, int, float]], set[int], set[int], list[int]]:
    """顺序直连：按 submission 分组；整表门禁通过的供应商按文档顺序 1:1 对齐、跳过 embedding，
    再做**逐行冲突隔离**——冲突行单独转 pending，不连累整表。

    整表门禁（每 submission 独立，全部满足才启用顺序直连）：
      1) 行数 == 锚点数；
      2) 锚点序号连续唯一；
      3) DN 覆盖率 ≥ 90%（双方都识别出 DN 的位置占比）；
      4) 已识别 DN 的同位置一致率 ≥ 95%；
      5) 归一化大类族(_coarse_family)同位置一致率 ≥ 90%（防整体/局部同DN串位；名称术语差异不否决）。
    逐行隔离（整表通过后，对每个位置）：
      - DN 冲突 / 大类族冲突 / 单位不一致 / 数量不一致 → 该行 pending（标 REVIEW）；
      - 其余行 align。
    报价行顺序：完整合法的 document_row_index 用它；全缺(历史)用载入顺序(submission_id,id)；
      残缺/非法(业务序号损坏)→拒绝顺序直连，回退语义。

    返回 (seq_matches[(qi,ai,score)], seq_qi 全部直连行, seq_conflict_qi 需 pending 行, embed_qi)。
    """
    if not anchors or not quotes:
        return [], set(), set(), list(range(len(quotes)))
    a_dn = _anchor_dns(anchors)
    a_fam = [_coarse_family(getattr(a, "name", "") or "") for a in anchors]
    order = sorted(range(len(anchors)), key=lambda ai: int(getattr(anchors[ai], "seq", 0) or 0))
    seqs = [int(getattr(anchors[ai], "seq", 0) or 0) for ai in order]
    continuous = bool(seqs) and len(set(seqs)) == len(seqs) and \
        seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    by_sub: dict[int, list[int]] = {}
    for qi, q in enumerate(quotes):
        by_sub.setdefault(q.supplier_id or 0, []).append(qi)

    seq_matches: list[tuple[int, int, float]] = []
    seq_qi: set[int] = set()
    seq_conflict_qi: set[int] = set()
    embed_qi: list[int] = []
    for sid, qis in by_sub.items():
        # 文档顺序：完整合法的 document_row_index 用它；全缺(历史)回退id顺序；残缺/非法→拒绝直连。
        qis, _reject = _doc_order(qis, doc_index)
        n = len(qis)
        if _reject or not (continuous and n == len(order)):
            embed_qi.extend(qis)
            continue
        # ── 逐位置评估 ──
        per_pos: list[tuple[int, int, bool]] = []   # (qi, ai, row_clean)
        dn_both = dn_match = fam_both = fam_match = 0
        for pos, qi in enumerate(qis):
            ai = order[pos]
            adn, qdn = a_dn[ai], quote_dns[qi]
            dn_present = bool(adn and qdn)
            dn_hit = dn_present and str(adn) == str(qdn)
            if dn_present:
                dn_both += 1; dn_match += dn_hit
            # 归一化大类族（稳健于名称变体；逐行+整表都用它防同DN串位）
            afam, qfam = a_fam[ai], _coarse_family(getattr(materials[qi], "standard_name", "") or "")
            fam_present = bool(afam and qfam)
            fam_hit = fam_present and afam == qfam
            if fam_present:
                fam_both += 1; fam_match += fam_hit
            a_unit = (getattr(anchors[ai], "unit", "") or "").strip()
            q_unit = (getattr(materials[qi], "unit", "") or "").strip()
            unit_conflict = bool(a_unit and q_unit and a_unit != q_unit)
            a_qty = getattr(anchors[ai], "qty", None)
            q_qty = getattr(quotes[qi], "quantity", None)
            qty_conflict = (a_qty is not None and q_qty is not None and not _qty_eq(a_qty, q_qty))
            # 逐行冲突：DN不符 / 大类族不符(防同DN异类) / 单位不符 / 数量不符 → 该行 pending
            row_conflict = (dn_present and not dn_hit) or (fam_present and not fam_hit) \
                or unit_conflict or qty_conflict
            per_pos.append((qi, ai, not row_conflict))
        # ── 整表门禁 ──
        dn_cov = dn_both / n if n else 0.0
        dn_cons = dn_match / dn_both if dn_both else 0.0
        fam_cons = fam_match / fam_both if fam_both else 1.0
        accept = (dn_cov >= _SEQ_DN_COVERAGE and dn_cons >= _SEQ_DN_CONSISTENCY
                  and fam_cons >= _SEQ_FAM_CONSISTENCY)
        if not accept:
            embed_qi.extend(qis)
            continue
        conflicts = 0
        for qi, ai, clean in per_pos:
            seq_matches.append((qi, ai, 1.0 if clean else 0.0))
            seq_qi.add(qi)
            if not clean:
                seq_conflict_qi.add(qi); conflicts += 1
        log.info("sequential direct-connect: submission=%s %d 行直连 (DN覆盖%.0f%% 一致%.0f%% 族%.0f%%, 冲突%d行→pending)",
                 sid, n, dn_cov * 100, dn_cons * 100, fam_cons * 100, conflicts)
    return seq_matches, seq_qi, seq_conflict_qi, embed_qi


def import_and_match(
    db: Session,
    xlsx_bytes: bytes | None,
    project_id: int,
    category: str,
    supplier_ids: list[int] | None = None,
    submission_ids: list[int] | None = None,
    anchors: list[TenderAnchor] | None = None,
    tender_list_session_id: int | None = None,
    brand_ctx: tuple[set, dict] | None = None,
) -> tuple[MatchSummary, dict]:
    """解析清单 + 嵌入匹配 + 落 BidAlignmentGroup。幂等:先清同 (project,category) 旧组。

    xlsx_bytes: raw xlsx content; ignored when anchors is provided.
    anchors: pre-built TenderAnchor list (from TenderListSession); takes priority.
    brand_ctx: (allowed_aliases, supplier_expected_aliases) 来自招标文件第13页，
        用于品牌硬信号校验（冲突→pending）。None 时跳过品牌校验。

    Returns:
        (MatchSummary, per_supplier_stats)
        per_supplier_stats: {supplier_id: {quote_rows, matched_rows, pending_rows,
                                           residue_rows, aggregated_rows}}
    """
    from apps.api.services.brand_match import check_brand

    if anchors is None:
        anchors = parse_tender_xlsx(xlsx_bytes)

    allowed_aliases, supplier_expected = brand_ctx or (set(), {})

    def _apply_brand(qt, sid: int, action: str, note: str) -> tuple[str, str]:
        """品牌硬信号：conflict→降级 pending；match/allowed→附加证据标记（不提升）。

        设计约束（不可违反）：
        - brand match 只是 evidence，不能覆盖 DN/规格/材质/名称冲突。
          canonical 硬过滤（c_score==0.0）发生在 _score_anchors_for_quote，
          brand match 在此之后，因此无法救回被 canonical 拒绝的候选。
        - conflict 只降级 pending，不 reject。别名表可能不全，宁可保守。
        """
        if not allowed_aliases:
            return action, note
        # For brand lookup, use actual supplier_id (not submission_id group key)
        brand_sid = getattr(qt, "actual_supplier_id", None) or (
            qt.supplier_id if not hasattr(qt, "actual_supplier_id") else None
        )
        verdict = check_brand(
            getattr(qt, "brand", "") or "",
            allowed_aliases,
            supplier_expected.get(brand_sid),
        )
        if verdict == "conflict":
            return "pending", (note + " brand_conflict").strip()
        if verdict == "match":
            return action, (note + " brand✓").strip()
        if verdict == "allowed":
            return action, (note + " brand~").strip()
        return action, note

    # ── 分离：哪些供应商已提交 BidSubmission（新路径），哪些走旧 Quote 路径 ──────
    # resolve_active_submissions now returns {submission_id → BidSubmission}
    from apps.api.services.bid_submission_resolve import resolve_active_submissions
    _active_subs = resolve_active_submissions(
        db, project_id, category,
        supplier_ids=supplier_ids,
        submission_ids=submission_ids,
    )
    # submission_ids of active BQL submissions
    bql_submission_ids: list[int] = list(_active_subs.keys())
    # supplier_ids that have BidSubmissions (for legacy exclusion)
    bql_supplier_ids: set[int] = {
        sub.supplier_id for sub in _active_subs.values() if sub.supplier_id
    }

    # ── 新路径：载入 BidQuoteLine（分品类） ───────────────────────────────────
    quotes: list = []
    materials: list = []
    is_bql_flags: list[bool] = []

    if bql_submission_ids:
        _bql_q = (
            db.query(BidQuoteLine, BidSubmission)
            .join(BidSubmission, BidQuoteLine.submission_id == BidSubmission.id)
            .filter(BidQuoteLine.submission_id.in_(bql_submission_ids))
        )
        if category:
            _bql_q = _bql_q.filter(BidQuoteLine.category == category)
        # 显式按 (submission, id) 排序 = 入库/文档顺序，供顺序直连按位置对齐（不依赖隐式顺序）。
        _bql_q = _bql_q.order_by(BidQuoteLine.submission_id.asc(), BidQuoteLine.id.asc())
        for bql, sub in _bql_q.all():
            _dri = None
            _meta = bql.extraction_meta or {}
            if isinstance(_meta, dict):
                _dri = _meta.get("document_row_index")
            quotes.append(_BQLProxy(
                id=bql.id,
                supplier_id=sub.id,          # group key = submission_id
                submission_id=sub.id,         # written to BidAlignmentItem.submission_id
                actual_supplier_id=sub.supplier_id,  # soft-ref to Supplier (can be None)
                total_price=bql.total_price,
                quantity=bql.qty,
                brand=bql.brand,
                canonical=bql.canonical,
                document_row_index=_dri,
            ))
            materials.append(_BQLMatProxy(
                standard_name=bql.standard_name,
                spec=bql.spec,
                unit=bql.unit,
            ))
            is_bql_flags.append(True)

    # ── 旧路径：载入 Quote（仅限无 BidSubmission 的供应商） ───────────────────
    bql_sup_ids = bql_supplier_ids
    _qt_q = (
        db.query(Quote, Material)
        .join(Material, Quote.material_id == Material.id)
        .filter(Quote.project_id == project_id)
    )
    if category:
        _qt_q = _qt_q.filter(Material.category == category)
    if supplier_ids:
        legacy_sids = [sid for sid in supplier_ids if sid not in bql_sup_ids]
        if not legacy_sids:
            # All requested suppliers have BidSubmissions — skip Quote query
            rows = []
        else:
            _qt_q = _qt_q.filter(Quote.supplier_id.in_(legacy_sids))
            rows = _qt_q.all()
    elif bql_sup_ids:
        # No supplier filter but some have BidSubmissions — exclude them from Quote
        _qt_q = _qt_q.filter(~Quote.supplier_id.in_(bql_sup_ids))
        rows = _qt_q.all()
    else:
        rows = _qt_q.all()

    for qt, m in rows:
        quotes.append(qt)
        materials.append(m)
        is_bql_flags.append(False)

    quote_texts = [f"{m.standard_name} {m.spec or ''}".strip() for m in materials]
    quote_dns = [_dn_of(m.spec) or _dn_of(m.standard_name) for m in materials]

    # Compute/load canonical per item (cache hit from extended_attrs or bql.canonical)
    quote_canonicals: list[dict] = []
    for i, m in enumerate(materials):
        if is_bql_flags[i]:
            bql_proxy = quotes[i]  # _BQLProxy
            bql_canon = bql_proxy.canonical or extract_valve_canonical(m.standard_name or "", m.spec or "")
            quote_canonicals.append(bql_canon or {})
        else:
            ext = (m.extended_attrs or {})
            canon = ext.get("canonical")
            if not canon:
                canon = extract_valve_canonical(m.standard_name or "", m.spec or "")
            quote_canonicals.append(canon or {})

    # 顺序直连优先：门禁通过的供应商按文档顺序 1:1 对齐，跳过 embedding；其余走语义。
    # doc_index：优先用持久化的 document_row_index（extraction_meta），否则回退载入顺序。
    _doc_index: dict[int, int] = {}
    for _qi, _qt in enumerate(quotes):
        if is_bql_flags[_qi]:
            _dri = (getattr(_qt, "document_row_index", None))
            if _dri is not None:
                _doc_index[_qi] = _dri
    seq_matches, seq_qi, seq_conflict_qi, embed_qi = _sequential_matches(
        anchors, quotes, materials, quote_dns, quote_canonicals,
        doc_index=_doc_index or None)
    embed_matches: list[tuple[int, int, float]] = []
    if embed_qi:
        _sub_quotes = [quotes[i] for i in embed_qi]
        _sub_texts = [quote_texts[i] for i in embed_qi]
        _sub_dns = [quote_dns[i] for i in embed_qi]
        _sub_canon = [quote_canonicals[i] for i in embed_qi]
        for _sqi, _ai, _cos in match_anchors(
            anchors, _sub_quotes, _sub_texts, _sub_dns, quote_canonicals=_sub_canon
        ):
            embed_matches.append((embed_qi[_sqi], _ai, _cos))
    matches = seq_matches + embed_matches

    # Persist canonical to Material.extended_attrs (cache only; don't overwrite; skip BQL proxies)
    for mi, m in enumerate(materials):
        if is_bql_flags[mi]:
            continue  # _BQLMatProxy has no DB-backed extended_attrs
        canon = quote_canonicals[mi]
        if canon.get("valve_type"):
            ext = dict(m.extended_attrs or {})
            if "canonical" not in ext:
                ext["canonical"] = canon
                m.extended_attrs = ext

    # 预载有效 supplier_id 集合，避免向已删除供应商插外键
    valid_sids: set[int] = {row[0] for row in db.query(Supplier.id).all()}

    # 幂等:清掉本 (project,category) 既有对齐组(及级联 items)
    old = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
    ).all()
    for g in old:
        db.delete(g)
    db.flush()

    # per_supplier_stats: init all suppliers with their total quote counts
    per_supplier_stats: dict[int, dict] = {}
    for qi, qt in enumerate(quotes):
        sid = qt.supplier_id or 0
        if sid not in per_supplier_stats:
            per_supplier_stats[sid] = {
                "supplier_id": sid,
                "quote_rows": 0,
                "matched_rows": 0,
                "pending_rows": 0,
                "residue_rows": 0,
                "aggregated_rows": 0,
                "computed_total": 0.0,
                "validation_failed_rows": 0,
                "cross_type_conflicts": 0,
            }
        per_supplier_stats[sid]["quote_rows"] += 1
        if qt.total_price is not None:
            per_supplier_stats[sid]["computed_total"] += float(qt.total_price)
        m = materials[qi]
        if (m.extended_attrs or {}).get("validation_warning"):
            per_supplier_stats[sid]["validation_failed_rows"] += 1

    # Track which quote indices were matched
    matched_qi: set[int] = set()
    # by_anchor_supplier[ai][sid] = [(qi, cos), ...]
    by_anchor_supplier: dict[int, dict[int, list[tuple[int, float]]]] = {}
    for qi, ai, cos in matches:
        sid = quotes[qi].supplier_id or 0
        by_anchor_supplier.setdefault(ai, {}).setdefault(sid, []).append((qi, cos))
        matched_qi.add(qi)

    # Unmatched quotes → residue
    for qi, qt in enumerate(quotes):
        if qi not in matched_qi:
            sid = qt.supplier_id or 0
            per_supplier_stats[sid]["residue_rows"] += 1

    low_conf = 0

    for ai, supplier_groups in by_anchor_supplier.items():
        a = anchors[ai]
        spec = " ".join(x for x in [a.spec, a.pressure, a.material_text()] if x).strip()

        # Group representative confidence: min cosine across all quotes
        all_cos = [cos for items in supplier_groups.values() for _, cos in items]
        min_cos = min(all_cos) if all_cos else 0.0

        # Groups are always "confirmed" — pending moves to item level
        group = BidAlignmentGroup(
            project_id=project_id,
            category=category,
            suggested_name=a.name,
            suggested_spec=spec,
            suggested_unit=a.unit,
            suggested_qty=a.qty,
            confidence=round(min_cos, 3),
            reason=f"招标清单锚点 #{a.seq}",
            status="confirmed",
            tender_list_session_id=tender_list_session_id,
            anchor_seq=str(a.seq),
        )
        db.add(group)
        db.flush()

        seen_qids: set[int] = set()

        for sid, items in supplier_groups.items():
            if len(items) <= 1:
                # Single row: item action depends on individual cosine
                qi, cos = items[0]
                if qi in seen_qids:
                    continue
                seen_qids.add(qi)
                _is_seq = qi in seq_qi
                _seq_conflict = qi in seq_conflict_qi
                if cos < LOW_CONF and not _is_seq:
                    low_conf += 1
                # 顺序直连：clean 行 align；逐行冲突行（DN/类型/单位/数量不一致）→ pending（REVIEW）。
                if _is_seq:
                    item_action = "pending" if _seq_conflict else "align"
                else:
                    item_action = "align" if cos >= LOW_CONF else "pending"
                qt = quotes[qi]
                _actual_sid = getattr(qt, "actual_supplier_id", qt.supplier_id)
                qsid = _actual_sid if _actual_sid in valid_sids else None
                canon_snap = quote_canonicals[qi] if qi < len(quote_canonicals) else {}
                if _is_seq:
                    note = f"position_direct seq={a.seq}"
                    if _seq_conflict:
                        note += " 字段冲突待核(DN/类型/单位/数量)"
                else:
                    note = f"cos={cos:.2f}"
                    if canon_snap.get("valve_type"):
                        note += f" {canon_snap.get('valve_type')} {canon_snap.get('dn','')} {canon_snap.get('pn','')}"
                item_action, note = _apply_brand(qt, sid, item_action, note)
                _qi_bql = is_bql_flags[qi]
                _sub_id = qt.submission_id if _qi_bql else None
                db.add(BidAlignmentItem(
                    group_id=group.id,
                    quote_id=None if _qi_bql else qt.id,
                    bid_quote_line_id=qt.id if _qi_bql else None,
                    supplier_id=qsid,
                    submission_id=_sub_id,
                    action=item_action,
                    spec_note=note.strip(),
                ))
                if item_action == "align":
                    per_supplier_stats[sid]["matched_rows"] += 1
                else:
                    per_supplier_stats[sid]["pending_rows"] += 1
            else:
                # Multiple rows from same supplier to same anchor
                # Group by canonical+unit: same key → aggregate; different keys → each pending
                canon_key_groups: dict[tuple, list[tuple[int, float]]] = {}
                for qi, cos in items:
                    if qi in seen_qids:
                        continue
                    qc = quote_canonicals[qi] if qi < len(quote_canonicals) else {}
                    unit_str = (materials[qi].unit or "") if qi < len(materials) else ""
                    key = (qc.get("valve_type", ""), qc.get("dn", ""), qc.get("pn", ""), unit_str)
                    canon_key_groups.setdefault(key, []).append((qi, cos))

                # Multiple distinct canonical keys → conflicting specs, all items pending
                has_conflict = len(canon_key_groups) > 1

                for key, key_items in canon_key_groups.items():
                    fresh = [(qi, cos) for qi, cos in key_items if qi not in seen_qids]
                    if not fresh:
                        continue
                    best_qi, best_cos = max(fresh, key=lambda x: x[1])
                    if best_cos < LOW_CONF:
                        low_conf += 1
                    seen_qids.update(qi for qi, _ in fresh)

                    # Conflicting specs OR low confidence → pending; otherwise align
                    if has_conflict or best_cos < LOW_CONF:
                        item_action = "pending"
                    else:
                        item_action = "align"

                    qt = quotes[best_qi]
                    _actual_sid2 = getattr(qt, "actual_supplier_id", qt.supplier_id)
                    qsid = _actual_sid2 if _actual_sid2 in valid_sids else None
                    canon_snap = quote_canonicals[best_qi] if best_qi < len(quote_canonicals) else {}
                    note = f"cos={best_cos:.2f}"
                    if canon_snap.get("valve_type"):
                        note += f" {canon_snap.get('valve_type')} {canon_snap.get('dn','')} {canon_snap.get('pn','')}"
                    if has_conflict:
                        note += " (规格冲突,需确认)"

                    # Aggregate total/qty for same-canonical items (only when no conflict)
                    agg_total_val: float | None = None
                    agg_qty_val: float | None = None
                    if not has_conflict and len(fresh) > 1:
                        totals = [float(quotes[qi].total_price) for qi, _ in fresh
                                  if quotes[qi].total_price is not None]
                        qtys = [float(quotes[qi].quantity) for qi, _ in fresh
                                if quotes[qi].quantity is not None]
                        if totals:
                            agg_total_val = sum(totals)
                        if qtys:
                            agg_qty_val = sum(qtys)
                        agg_ids = [quotes[qi].id for qi, _ in fresh if qi != best_qi]
                        if agg_ids:
                            note += f" aggregated={agg_ids}"
                            per_supplier_stats[sid]["aggregated_rows"] += len(agg_ids)

                    item_action, note = _apply_brand(qt, sid, item_action, note)
                    _best_bql = is_bql_flags[best_qi]
                    _best_sub_id = qt.submission_id if _best_bql else None
                    db.add(BidAlignmentItem(
                        group_id=group.id,
                        quote_id=None if _best_bql else qt.id,
                        bid_quote_line_id=qt.id if _best_bql else None,
                        supplier_id=qsid,
                        submission_id=_best_sub_id,
                        action=item_action,
                        spec_note=note.strip(),
                        agg_total=agg_total_val,
                        agg_qty=agg_qty_val,
                    ))
                    if item_action == "align":
                        per_supplier_stats[sid]["matched_rows"] += 1
                    else:
                        per_supplier_stats[sid]["pending_rows"] += 1

    db.commit()

    # 指标
    anchor_suppliers: dict[int, set[int]] = {}
    for qi, ai, _ in matches:
        anchor_suppliers.setdefault(ai, set()).add(quotes[qi].supplier_id)

    summary = MatchSummary(
        anchors_total=len(anchors),
        anchors_covered=len(anchor_suppliers),
        comparable_2plus=sum(1 for s in anchor_suppliers.values() if len(s) >= 2),
        three_way=sum(1 for s in anchor_suppliers.values() if len(s) >= 3),
        matched_quotes=len(matches),
        total_quotes=len(quotes),
        low_conf=low_conf,
        residue=len(quotes) - len(matched_qi),
    )
    return summary, per_supplier_stats
