"""QuoteRound — one round of quote collection for (project, category).

docs/design/42. A project may collect quotes several times — a pre-tender
sounding round, then round 1 / 2 / 3 of the formal tender. Each round is a
complete, independently-comparable quote set:

  - `BidSubmission.round_id` attaches every uploaded quote to exactly one round.
  - `confirmed_supplier_ids` / `used_submission_ids` are the round's own copy
    of the comparison scope that `TenderListSession` used to carry alone (see
    docs/design/42 §3.1) — a later round must never overwrite an earlier
    round's scope.
  - `is_final_basis` is set only by an explicit user action (docs/design/42
    §8 D3). No round is auto-promoted. Official evaluation/export/
    recommendation must refuse to run without one.

`round_id` on `BidAlignmentGroup` and the corresponding re-scoped match
wipe/rebuild are P2 work (see docs/design/42 §4.1) — not present yet, so a
second round's `/tender-list/match` still overwrites the first round's
alignment rows. This model only stores rounds and their scope; it does not
yet protect a prior round's alignment result.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now

STAGE_PRE_TENDER = "pre_tender"
STAGE_FORMAL = "formal"
STAGES = (STAGE_PRE_TENDER, STAGE_FORMAL)

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_OPEN, STATUS_CLOSED)


class QuoteRound(Base):
    """One round of quote collection within (project_id, category)."""

    __tablename__ = "quote_rounds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    category = Column(String(50), nullable=False, default="")

    seq = Column(Integer, nullable=False)               # 1, 2, 3 … within (project_id, category)
    name = Column(String(200), nullable=False, default="")  # user-authored label, e.g. "第一轮"

    stage = Column(String(20), nullable=False, default=STAGE_FORMAL)
    status = Column(String(20), nullable=False, default=STATUS_OPEN)

    # Explicit only — docs/design/42 §8 D3. Never auto-promoted on round creation.
    is_final_basis = Column(Boolean, nullable=False, default=False)

    # Provenance: which confirmed list version this round was matched against.
    # Not part of round identity — the list can be revised without ending the round.
    tender_list_session_id = Column(
        Integer, ForeignKey("tender_list_sessions.id"), nullable=True,
    )

    # Comparison scope — this round's own copy, previously the sole property of
    # TenderListSession (docs/design/42 §3.1). list[int].
    confirmed_supplier_ids = Column(JSON, nullable=True)
    used_submission_ids = Column(JSON, nullable=True)

    created_by = Column(String(100), nullable=True)
    remark = Column(Text, nullable=False, default="")

    opened_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    submissions = relationship("BidSubmission", back_populates="round")

    __table_args__ = (
        Index("ix_quote_rounds_project_category", "project_id", "category"),
        Index("ix_quote_rounds_project_category_seq", "project_id", "category", "seq", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<QuoteRound id={self.id} project_id={self.project_id} "
            f"category={self.category!r} seq={self.seq} name={self.name!r} "
            f"status={self.status} final_basis={self.is_final_basis}>"
        )
