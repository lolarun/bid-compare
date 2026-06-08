"""锚点匹配服务(docs/design/05 §9 第3步)。

把招标清单锚点行 + 供应商报价做嵌入语义匹配,落成 BidAlignmentGroup(锚点=组,
报价=组内 item),现有 bid-matrix 自动渲染成"锚点行 × 供应商"比价矩阵。

分层(本版到 Tier2):
  Tier1 嵌入召回:每条报价找余弦最近、DN 一致的锚点
  Tier2 DN 规则核对:DN 不一致的候选跳过
  Tier3 闸②LLM复核 + 缓存:暂缓(见设计文档 §9 决策 2026-06-08)

归一靠嵌入语义,**零硬编码同义词表**——对任何品类通用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy.orm import Session

from apps.api.core.config import get_settings
from apps.api.models import Material, Quote
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.supplier import Supplier
from apps.api.services.tender_list import TenderAnchor, parse_tender_xlsx

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


def match_anchors(
    anchors: list[TenderAnchor],
    quotes: list[Quote],
    quote_texts: list[str],
    quote_dns: list[int | None],
    client: OpenAI | None = None,
) -> list[tuple[int, int, float]]:
    """返回 [(quote_idx, anchor_idx, cosine), ...](未匹配的不在列表里)。"""
    if not anchors or not quotes:
        return []
    client = client or _embed_client()
    a_text = [f"{a.name} {a.spec} {a.pressure} {a.material_text()}".strip() for a in anchors]
    a_dn = [_dn_of(a.spec) or _dn_of(a.name) for a in anchors]

    A = _embed(client, a_text)
    Q = _embed(client, quote_texts)
    sims = _cosine_matrix(Q, A)

    result: list[tuple[int, int, float]] = []
    for qi in range(len(quotes)):
        ranked = sorted(range(len(anchors)), key=lambda ai: -sims[qi][ai])
        for ai in ranked:
            if sims[qi][ai] < SIM_THRESHOLD:
                break
            if quote_dns[qi] is not None and a_dn[ai] is not None and quote_dns[qi] != a_dn[ai]:
                continue  # DN 核对不过,看下一候选
            result.append((qi, ai, sims[qi][ai]))
            break
    return result


def import_and_match(
    db: Session,
    xlsx_bytes: bytes,
    project_id: int,
    category: str,
    supplier_ids: list[int] | None = None,
) -> MatchSummary:
    """解析清单 + 嵌入匹配 + 落 BidAlignmentGroup。幂等:先清同 (project,category) 旧组。"""
    anchors = parse_tender_xlsx(xlsx_bytes)

    # 载入报价
    q = (
        db.query(Quote, Material)
        .join(Material, Quote.material_id == Material.id)
        .filter(Quote.project_id == project_id)
    )
    if category:
        q = q.filter(Material.category == category)
    if supplier_ids:
        q = q.filter(Quote.supplier_id.in_(supplier_ids))
    rows = q.all()
    quotes = [qt for qt, _ in rows]
    quote_texts = [f"{m.standard_name} {m.spec or ''}".strip() for _, m in rows]
    quote_dns = [_dn_of(m.spec) or _dn_of(m.standard_name) for _, m in rows]

    matches = match_anchors(anchors, quotes, quote_texts, quote_dns)

    # 预载有效 supplier_id 集合，避免向已删除供应商插外键
    valid_sids: set[int] = {
        row[0] for row in db.query(Supplier.id).all()
    }

    # 幂等:清掉本 (project,category) 既有对齐组(及级联 items)
    old = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
    ).all()
    for g in old:
        db.delete(g)
    db.flush()

    # 按锚点聚合匹配
    by_anchor: dict[int, list[tuple[int, float]]] = {}
    for qi, ai, cos in matches:
        by_anchor.setdefault(ai, []).append((qi, cos))

    low_conf = 0
    for ai, items in by_anchor.items():
        a = anchors[ai]
        spec = " ".join(x for x in [a.spec, a.pressure, a.material_text()] if x).strip()
        min_cos = min(c for _, c in items)
        group = BidAlignmentGroup(
            project_id=project_id,
            category=category,
            suggested_name=a.name,
            suggested_spec=spec,
            suggested_unit=a.unit,
            suggested_qty=a.qty,
            confidence=round(min_cos, 3),
            reason=f"招标清单锚点 #{a.seq}" + ("(含低置信匹配,建议复核)" if min_cos < LOW_CONF else ""),
            status="confirmed",
        )
        db.add(group)
        db.flush()
        seen: set[int] = set()
        for qi, cos in items:
            qt = quotes[qi]
            if qt.id in seen:
                continue
            seen.add(qt.id)
            if cos < LOW_CONF:
                low_conf += 1
            sid = qt.supplier_id if qt.supplier_id in valid_sids else None
            db.add(BidAlignmentItem(
                group_id=group.id,
                quote_id=qt.id,
                supplier_id=sid,
                action="align",
                spec_note=f"cos={cos:.2f}",
            ))
    db.commit()

    # 指标
    anchor_suppliers: dict[int, set[int]] = {}
    for qi, ai, _ in matches:
        anchor_suppliers.setdefault(ai, set()).add(quotes[qi].supplier_id)
    return MatchSummary(
        anchors_total=len(anchors),
        anchors_covered=len(anchor_suppliers),
        comparable_2plus=sum(1 for s in anchor_suppliers.values() if len(s) >= 2),
        three_way=sum(1 for s in anchor_suppliers.values() if len(s) >= 3),
        matched_quotes=len(matches),
        total_quotes=len(quotes),
        low_conf=low_conf,
        residue=len(quotes) - len(matches),
    )
