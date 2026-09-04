"""Round-over-round price trend — docs/design/42 §6.

Consumes `build_anchor_matrix` output **per round**, does not recompute
matrix semantics — every number here is a delta between two matrices this
module did not build.

## Comparability (R3/R4, docs/design/42 §5)

A discount figure is only ever emitted when three things line up between the
two rounds being compared: **same `anchor_uid`, same `price_basis`, same
quantity**. `price_basis` differing (含税 vs 不含税) is the classic way to
manufacture a fake discount — a lower unclassified-basis number next to a
higher tax-inclusive one looks like savings and isn't. When any of the three
differs, the pair is marked `comparable=False` with a `reason`, and **no
discount percentage is computed** — not zero, not the raw price delta.

A supplier absent from a round is `participating=False`. Never zero-filled,
never interpolated as a discount of -100%. R4 exists because an absence is
not a price.

## Supplier identity across rounds

Column identity within one round is `BidSubmission.id` (matrix convention).
Across rounds it must be the underlying `Supplier.id` — trend asks "did this
company get cheaper", not "did this specific upload get cheaper". A
submission with no linked `supplier_id` (unresolved/unknown supplier) has no
identity to carry across rounds and is reported standalone, never fuzzy-
matched by name — `.claude/rules/bid-compare-backend.md` forbids treating
`supplier_id` and submission identity as interchangeable, and a name match
would be exactly that kind of interchange in reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session


@dataclass
class RowTrendPoint:
    """One (anchor_uid, supplier, round) observation."""

    anchor_uid: str
    round_id: int
    round_seq: int
    supplier_id: int | None
    supplier_name: str
    participating: bool
    unit_price: float | None = None
    total: float | None = None
    price_basis: str | None = None
    comparable_to_prev: bool = False
    round_over_round_discount_pct: float | None = None
    comparable_to_first: bool = False
    cumulative_discount_pct: float | None = None
    not_comparable_reason: str | None = None


@dataclass
class SupplierRoundSummary:
    """One (supplier, round) rollup."""

    supplier_id: int | None
    supplier_name: str
    round_id: int
    round_seq: int
    total: float | None
    round_over_round_discount_pct: float | None = None
    cumulative_discount_pct: float | None = None
    rank: int | None = None            # 1 = cheapest total this round, among participating suppliers
    rank_change: int | None = None     # negative = moved up (cheaper rank), vs previous round
    comparable_to_prev: bool = False
    not_comparable_reason: str | None = None


@dataclass
class RoundTrendResult:
    project_id: int
    category: str
    round_ids: list[int]                     # oldest first — the sequence this trend covers
    rows: list[RowTrendPoint] = field(default_factory=list)
    suppliers: list[SupplierRoundSummary] = field(default_factory=list)
    skipped_rounds: list[dict] = field(default_factory=list)  # {round_id, seq, reason} — no silent drop


def _round_over_round(prev: float | None, cur: float | None) -> float | None:
    """% cheaper than the previous round. Positive = cheaper. None if prev is 0/None."""
    if prev is None or cur is None or prev == 0:
        return None
    return round((prev - cur) / prev * 100, 2)


def compute_round_trend(db: Session, project_id: int, category: str) -> RoundTrendResult:
    """Build the cross-round trend for (project_id, category).

    Walks every `QuoteRound` in seq order, builds each round's own matrix via
    `build_anchor_matrix(..., round_id=round.id)` scoped to that round's own
    `used_submission_ids`/`tender_list_session_id` (never `TenderListSession`'s
    shared, latest-round-wins fields — that is exactly the bug docs/design/42
    §3.1 moved this data off of), then stitches rows across rounds by
    `anchor_uid` and suppliers by `Supplier.id`.
    """
    from apps.api.models.tender_list_session import TenderListSession
    from apps.api.services.matrix.bid_matrix import build_anchor_matrix
    from apps.api.services.tender import quote_round_service
    from apps.api.services.tender.tender_list import rebuild_anchors

    rounds = sorted(
        quote_round_service.list_rounds(db, project_id, category),
        key=lambda r: r.seq,
    )

    result = RoundTrendResult(project_id=project_id, category=category, round_ids=[])

    # (anchor_uid, supplier_id) → [(round_seq, unit_price, total, price_basis), ...]
    row_history: dict[tuple[str, int | None], list[tuple[int, float | None, float | None, str | None]]] = {}
    # supplier_id → [(round_seq, total), ...]
    supplier_history: dict[int | None, list[tuple[int, float | None]]] = {}
    supplier_names: dict[int | None, str] = {}

    for rnd in rounds:
        if not rnd.used_submission_ids:
            result.skipped_rounds.append({
                "round_id": rnd.id, "seq": rnd.seq,
                "reason": "本轮未记录对齐范围（match 从未针对本轮跑过）",
            })
            continue
        if not rnd.tender_list_session_id:
            result.skipped_rounds.append({
                "round_id": rnd.id, "seq": rnd.seq,
                "reason": "本轮没有关联的采购清单版本，无法重建锚点",
            })
            continue
        session = db.get(TenderListSession, rnd.tender_list_session_id)
        if session is None or not session.anchors_json:
            result.skipped_rounds.append({
                "round_id": rnd.id, "seq": rnd.seq,
                "reason": "采购清单版本已不可用",
            })
            continue

        anchors = rebuild_anchors(session)
        matrix = build_anchor_matrix(
            db,
            anchors=anchors,
            tender_list_session_id=session.id,
            used_submission_ids=list(rnd.used_submission_ids or []),
            supplier_ids=[],
            submission_ids=[],
            project_id=project_id,
            category=category,
            round_id=rnd.id,
        )

        result.round_ids.append(rnd.id)

        # submission_id → supplier_id / display name, from this round's own columns
        sub_to_supplier: dict[int, int | None] = {}
        for label in matrix.get("suppliers", []):
            sub_to_supplier[label["id"]] = label.get("supplier_id")
            supplier_names.setdefault(label.get("supplier_id"), label.get("name") or "")

        for row in matrix.get("rows", []):
            a_uid = row.get("anchor_uid") or ""
            if not a_uid:
                continue  # 早于 P1 的锚点没有 anchor_uid，无法跨轮次连接，诚实跳过而非瞎连
            for cell in row.get("suppliers", []):
                # submission 模式下 "id" 就是 submission_id（B3 约定），round_trend
                # 只在 used_submission_ids 非空时调 build_anchor_matrix，恒为此模式。
                sub_id = cell.get("id")
                sup_id = sub_to_supplier.get(sub_id)
                if cell.get("cell_status") not in ("quoted", "aggregated"):
                    continue  # pending/excluded/missing 不是"参与了这一轮"的报价事实
                key = (a_uid, sup_id)
                row_history.setdefault(key, []).append(
                    (rnd.seq, cell.get("price"), cell.get("total"), cell.get("price_basis"))
                )

        for total_row in matrix.get("totals", []):
            # totals 的列身份键 "id" 与 suppliers 标签的 "id" 同一套（B3：submission
            # 模式下都等于 submission_id）——MatrixTotal 本身不带 supplier_id 字段，
            # 必须经 sub_to_supplier 这张本轮自己的映射表转一次，不能直接假设同名。
            sub_id = total_row.get("id") if isinstance(total_row, dict) else None
            sup_id = sub_to_supplier.get(sub_id)
            supplier_history.setdefault(sup_id, []).append(
                (rnd.seq, total_row.get("total") if isinstance(total_row, dict) else None)
            )

    # ── Row level: fill comparable/discount per §5 R3 ─────────────────────────
    for (a_uid, sup_id), points in row_history.items():
        points.sort(key=lambda p: p[0])
        first_price_basis = points[0][3]
        first_total = points[0][2]
        prev = points[0]
        for i, (seq, price, total, basis) in enumerate(points):
            round_obj = next((r for r in rounds if r.seq == seq), None)
            pt = RowTrendPoint(
                anchor_uid=a_uid,
                round_id=round_obj.id if round_obj else -1,
                round_seq=seq,
                supplier_id=sup_id,
                supplier_name=supplier_names.get(sup_id, ""),
                participating=True,
                unit_price=price,
                total=total,
                price_basis=basis,
            )
            if i > 0:
                prev_seq, prev_price, prev_total, prev_basis = prev
                if basis == prev_basis and basis is not None:
                    pt.comparable_to_prev = True
                    pt.round_over_round_discount_pct = _round_over_round(prev_total, total)
                else:
                    pt.not_comparable_reason = (
                        f"计税口径不同（上轮 {prev_basis or '未知'} vs 本轮 {basis or '未知'}），不计算折扣"
                    )
                if basis == first_price_basis and basis is not None:
                    pt.comparable_to_first = True
                    pt.cumulative_discount_pct = _round_over_round(first_total, total)
                elif pt.not_comparable_reason is None:
                    pt.not_comparable_reason = (
                        f"与首轮计税口径不同（首轮 {first_price_basis or '未知'} vs 本轮 {basis or '未知'}），不计算累计折扣"
                    )
            prev = (seq, price, total, basis)
            result.rows.append(pt)

    # ── Supplier level ─────────────────────────────────────────────────────
    for sup_id, points in supplier_history.items():
        points.sort(key=lambda p: p[0])
        first_total = points[0][1]
        prev_total = points[0][1]
        for seq, total in points:
            round_obj = next((r for r in rounds if r.seq == seq), None)
            summary = SupplierRoundSummary(
                supplier_id=sup_id,
                supplier_name=supplier_names.get(sup_id, ""),
                round_id=round_obj.id if round_obj else -1,
                round_seq=seq,
                total=total,
            )
            if seq != points[0][0]:
                summary.comparable_to_prev = total is not None and prev_total is not None
                if summary.comparable_to_prev:
                    summary.round_over_round_discount_pct = _round_over_round(prev_total, total)
                else:
                    summary.not_comparable_reason = "本轮或上一轮无有效总价"
                if total is not None and first_total is not None:
                    summary.cumulative_discount_pct = _round_over_round(first_total, total)
            result.suppliers.append(summary)
            prev_total = total

    # ── Rank + rank movement, within each round independently ────────────────
    by_round: dict[int, list[SupplierRoundSummary]] = {}
    for s in result.suppliers:
        by_round.setdefault(s.round_seq, []).append(s)
    prev_ranks: dict[int | None, int] = {}
    for seq in sorted(by_round):
        ranked = sorted(
            (s for s in by_round[seq] if s.total is not None),
            key=lambda s: s.total,
        )
        cur_ranks: dict[int | None, int] = {}
        for i, s in enumerate(ranked, start=1):
            s.rank = i
            cur_ranks[s.supplier_id] = i
            if s.supplier_id in prev_ranks:
                s.rank_change = i - prev_ranks[s.supplier_id]
        prev_ranks = cur_ranks

    return result


def round_trend_to_dict(result: RoundTrendResult) -> dict:
    """Serialize for the API response — dataclasses aren't JSON-native."""
    from dataclasses import asdict
    return {
        "project_id": result.project_id,
        "category": result.category,
        "round_ids": result.round_ids,
        "rows": [asdict(r) for r in result.rows],
        "suppliers": [asdict(s) for s in result.suppliers],
        "skipped_rounds": result.skipped_rounds,
    }
