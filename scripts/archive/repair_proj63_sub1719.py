"""One-off repair: restore sub17/18/19 to pending, supersede sub20-25.

Project 63 / 阀门:
  sub17 (上海绵存, supplier_id=None): superseded → pending
  sub18 (泰科龙,   supplier_id=None): superseded → pending
  sub19 (凯硕新正, supplier_id=72):   superseded → pending
  sub20-25:                           pending    → superseded

  TLS id=20: used_submission_ids = []
  BidAlignmentGroup + items where tender_list_session_id=20: deleted (74 groups)
  AlignmentFinalization: no records exist, no-op

CRITICAL:
  sub17 must remain supplier_id=NULL (never link to supplier 74).
  sub23 has supplier_id=74 — this script supersedes it; that constraint is NOT
  hardcoded in production logic, only enforced here as a data fix.
"""

import sys
import json

sys.path.insert(0, ".")  # run from repo root

from apps.api.core.database import SessionLocal
from apps.api.models.bid_submission import BidSubmission
from apps.api.models.tender_list_session import TenderListSession
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.alignment_finalization import AlignmentFinalization

RESTORE_TO_PENDING   = [17, 18, 19]
SUPERSEDE            = [20, 21, 22, 23, 24, 25]
TLS_ID               = 20
PROJECT_ID           = 63

def run(dry_run: bool = True):
    db = SessionLocal()
    try:
        # ── Pre-flight checks ─────────────────────────────────────────────────
        subs = {
            s.id: s
            for s in db.query(BidSubmission).filter(
                BidSubmission.id.in_(RESTORE_TO_PENDING + SUPERSEDE)
            ).all()
        }
        for sid in RESTORE_TO_PENDING + SUPERSEDE:
            assert sid in subs, f"BidSubmission id={sid} not found!"

        for sid in RESTORE_TO_PENDING:
            s = subs[sid]
            assert s.project_id == PROJECT_ID, f"sub{sid} project_id={s.project_id} ≠ 63"
            # sub17 must never have supplier_id=74
            if sid == 17:
                assert s.supplier_id != 74, f"sub17 has forbidden supplier_id=74!"

        tls = db.get(TenderListSession, TLS_ID)
        assert tls is not None, f"TLS id={TLS_ID} not found!"
        assert tls.project_id == PROJECT_ID

        groups = db.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.tender_list_session_id == TLS_ID
        ).all()
        group_ids = [g.id for g in groups]
        item_count = db.query(BidAlignmentItem).filter(
            BidAlignmentItem.group_id.in_(group_ids)
        ).count() if group_ids else 0

        fin_count = db.query(AlignmentFinalization).filter(
            AlignmentFinalization.project_id == PROJECT_ID
        ).count()

        # ── Dry-run report ────────────────────────────────────────────────────
        print("=" * 60)
        print(f"{'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
        print("=" * 60)
        for sid in RESTORE_TO_PENDING:
            s = subs[sid]
            print(f"  sub{sid} ({s.supplier_raw_name!r}, supplier_id={s.supplier_id}): "
                  f"{s.status!r} → 'pending'")
        for sid in SUPERSEDE:
            s = subs[sid]
            print(f"  sub{sid} ({s.supplier_raw_name!r}, supplier_id={s.supplier_id}): "
                  f"{s.status!r} → 'superseded'")
        print(f"  TLS id={TLS_ID}: used_submission_ids={tls.used_submission_ids!r} → []")
        print(f"  Delete {len(group_ids)} BidAlignmentGroup(s), {item_count} item(s) "
              f"(all under tender_list_session_id={TLS_ID})")
        print(f"  AlignmentFinalization: {fin_count} record(s) for project 63 — no-op")
        print("=" * 60)

        if dry_run:
            print("Dry-run complete. Pass --live to execute.")
            return

        # ── Execute (single transaction) ──────────────────────────────────────
        for sid in RESTORE_TO_PENDING:
            subs[sid].status = "pending"

        for sid in SUPERSEDE:
            subs[sid].status = "superseded"

        # Delete alignment items first (FK)
        if group_ids:
            db.query(BidAlignmentItem).filter(
                BidAlignmentItem.group_id.in_(group_ids)
            ).delete(synchronize_session=False)
            db.query(BidAlignmentGroup).filter(
                BidAlignmentGroup.id.in_(group_ids)
            ).delete(synchronize_session=False)

        tls.used_submission_ids = []

        db.commit()
        print("Transaction committed.")

        # ── Post-commit verification ──────────────────────────────────────────
        db.expire_all()
        for sid in RESTORE_TO_PENDING:
            s = db.get(BidSubmission, sid)
            assert s.status == "pending", f"sub{sid} status={s.status!r} expected 'pending'"
            print(f"  ✓ sub{sid} status={s.status!r}")
        for sid in SUPERSEDE:
            s = db.get(BidSubmission, sid)
            assert s.status == "superseded", f"sub{sid} status={s.status!r} expected 'superseded'"
            print(f"  ✓ sub{sid} status={s.status!r}")
        remaining_groups = db.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.tender_list_session_id == TLS_ID
        ).count()
        assert remaining_groups == 0, f"{remaining_groups} groups still exist!"
        print(f"  ✓ BidAlignmentGroup count under TLS={TLS_ID}: 0")
        tls_check = db.get(TenderListSession, TLS_ID)
        assert (tls_check.used_submission_ids or []) == [], \
            f"used_submission_ids={tls_check.used_submission_ids!r}"
        print(f"  ✓ TLS id={TLS_ID} used_submission_ids=[]")
        print("All assertions passed. Repair complete.")

    except Exception as exc:
        db.rollback()
        print(f"\nERROR — rolled back: {exc}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    dry = "--live" not in sys.argv
    run(dry_run=dry)
