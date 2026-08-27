"""docs/design/42 P1 — anchor_uid: a stable anchor identity that survives a
procurement-list revision.

Backfills `anchor_uid` into every existing `tender_list_sessions.anchors_json`
entry, walking each (project_id, category)'s version chain oldest-first so a
row that didn't change keeps the same id its predecessor had — the same
content-match rule `tender_session_service._assign_anchor_uids` applies to
new writes going forward (duplicated here rather than imported: a migration
must keep working even if the app-side algorithm changes later).

Does NOT touch `bid_alignment_groups` — that's P2, together with the
round-scoped re-match fix (docs/design/42 §4.1).

Revision ID: 0010_anchor_uid
Revises: 0009_quote_rounds
"""
from __future__ import annotations

import json
import uuid

from alembic import op
from sqlalchemy import text


revision = "0010_anchor_uid"
down_revision = "0009_quote_rounds"
branch_labels = None
depends_on = None


def _identity(a: dict) -> tuple[str, str]:
    return (str(a.get("name") or "").strip(), str(a.get("spec") or "").strip())


def _assign(anchors: list, prev_anchors: list | None) -> list:
    prev_by_identity: dict[tuple[str, str], list[str]] = {}
    for pa in (prev_anchors or []):
        if not isinstance(pa, dict):
            continue
        uid = str(pa.get("anchor_uid") or "")
        if uid:
            prev_by_identity.setdefault(_identity(pa), []).append(uid)

    out = []
    for a in anchors:
        if not isinstance(a, dict):
            out.append(a)
            continue
        candidates = prev_by_identity.get(_identity(a)) or []
        uid = candidates.pop(0) if candidates else uuid.uuid4().hex
        out.append({**a, "anchor_uid": uid})
    return out


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(text(
        "SELECT id, project_id, category, version, anchors_json FROM tender_list_sessions "
        "ORDER BY project_id, category, version ASC"
    )).fetchall()
    if not rows:
        return

    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault((row[1], row[2]), []).append(row)

    for _, chain in groups.items():
        prev_anchors: list | None = None
        for session_id, _pid, _cat, _version, anchors_raw in chain:
            anchors = json.loads(anchors_raw) if isinstance(anchors_raw, str) else (anchors_raw or [])
            if not isinstance(anchors, list):
                continue
            already_done = anchors and all(
                isinstance(a, dict) and a.get("anchor_uid") for a in anchors
            )
            if already_done:
                prev_anchors = anchors
                continue

            new_anchors = _assign(anchors, prev_anchors)
            conn.execute(
                text("UPDATE tender_list_sessions SET anchors_json = :aj WHERE id = :id"),
                {"aj": json.dumps(new_anchors, ensure_ascii=False), "id": session_id},
            )
            prev_anchors = new_anchors


def downgrade() -> None:
    """No-op — anchor_uid is additive data inside a JSON column; stripping it
    back out row-by-row isn't worth the risk of touching every session's
    anchors_json a second time for a downgrade path nothing exercises."""
