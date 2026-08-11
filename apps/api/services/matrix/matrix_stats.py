"""
Compute MatrixDistribution from build_anchor_matrix() rows.

Both /llm-fill and /bid-matrix call build_matrix_distribution_from_rows()
on the same DB-backed rows so the two endpoints are always same-source.
"""

from __future__ import annotations

QUOTED_STATUSES = frozenset({"quoted", "aggregated"})
COVERED_STATUSES = frozenset({"quoted", "aggregated", "pending"})


def build_matrix_distribution_from_rows(rows: list[dict], supplier_ids: list[int]) -> dict:
    """Build distribution from build_anchor_matrix() rows.

    Each row["suppliers"] is a per-supplier cell list that already includes
    missing cells, so no dedup or priority logic is needed here.
    """
    N = len(supplier_ids)
    q_dist: dict[int, int] = {k: 0 for k in range(N + 1)}
    c_dist: dict[int, int] = {k: 0 for k in range(N + 1)}
    for row in rows:
        qc = sum(1 for c in row["suppliers"] if c.get("cell_status") in QUOTED_STATUSES)
        cc = sum(1 for c in row["suppliers"] if c.get("cell_status") in COVERED_STATUSES)
        q_dist[qc] += 1
        c_dist[cc] += 1
    anchors_total = len(rows)
    return {
        "supplier_count": N,
        "anchors_total": anchors_total,
        "quoted_distribution": {str(k): q_dist[k] for k in range(N + 1)},
        "covered_distribution": {str(k): c_dist[k] for k in range(N + 1)},
        "quoted_ge_2_count": sum(v for k, v in q_dist.items() if k >= 2),
        "quoted_full_count": q_dist[N],
        "covered_ge_2_count": sum(v for k, v in c_dist.items() if k >= 2),
        "covered_full_count": c_dist[N],
    }


def build_matrix_distribution_from_cells(
    cells: list[tuple[int, int, str]],
    anchor_seqs: list[int],
    supplier_ids: list[int],
) -> dict:
    """Debug/fallback: build distribution from in-memory (anchor_seq, supplier_id, status) tuples.

    When the same (anchor_seq, supplier_id) pair appears multiple times, the
    highest-priority status wins: quoted/aggregated > pending > excluded > missing.
    Not used by any API endpoint — only for offline analysis.
    """
    PRIORITY = {"quoted": 0, "aggregated": 0, "pending": 1, "excluded": 2, "missing": 3}
    best: dict[tuple[int, int], str] = {}
    for seq, sid, st in cells:
        key = (seq, sid)
        if key not in best or PRIORITY.get(st, 9) < PRIORITY.get(best[key], 9):
            best[key] = st

    N = len(supplier_ids)
    q_dist: dict[int, int] = {k: 0 for k in range(N + 1)}
    c_dist: dict[int, int] = {k: 0 for k in range(N + 1)}
    for seq in anchor_seqs:
        qc = sum(1 for sid in supplier_ids if best.get((seq, sid), "missing") in QUOTED_STATUSES)
        cc = sum(1 for sid in supplier_ids if best.get((seq, sid), "missing") in COVERED_STATUSES)
        q_dist[qc] += 1
        c_dist[cc] += 1
    anchors_total = len(anchor_seqs)
    return {
        "supplier_count": N,
        "anchors_total": anchors_total,
        "quoted_distribution": {str(k): q_dist[k] for k in range(N + 1)},
        "covered_distribution": {str(k): c_dist[k] for k in range(N + 1)},
        "quoted_ge_2_count": sum(v for k, v in q_dist.items() if k >= 2),
        "quoted_full_count": q_dist[N],
        "covered_ge_2_count": sum(v for k, v in c_dist.items() if k >= 2),
        "covered_full_count": c_dist[N],
    }
