"""
repair_project63.py — 修复 project 63 / 阀门 比价数据污染

背景：
  batch_confirm 传入 category="" → BQL 全部跳过 → 空壳 BidSubmission
  → import_and_match 回退 legacy quotes → 矩阵污染（伯尔梅特 ¥0 "推荐"）

本次修复严格使用：
  submission 17 → 上海绵存（supplier_id=NULL，未经证明不关联 supplier 74）
  submission 18 → 泰科龙（supplier_id=NULL，supplier_raw_name 修正为"泰科龙"）
  submission 19 → 凯硕新正（supplier_id=72）
  **禁止**使用 submission 14（旧泰科龙上传，请标记 superseded）

执行顺序（原子事务）：
  Phase 1：为 17/18/19 重建 BidQuoteLine（同一事务，不提交）
  Phase 2：内存校验三者均有 BQL、category 正确、名称正确
    全部通过 → Phase 3 清理后统一 commit
    任何失败 → rollback，数据库恢复原状

清理范围：仅删除 tender_list_session_id=17 的 BidAlignmentGroup（及 Item）
         将 17/18/19 以外的所有 submission 标记为 status='superseded'

运行（在 repo 根目录）：
  .venv\\Scripts\\python.exe -X utf8 scripts\\repair_project63.py --dry-run
  .venv\\Scripts\\python.exe -X utf8 scripts\\repair_project63.py
  .venv\\Scripts\\python.exe -X utf8 scripts\\repair_project63.py --verify
"""
import argparse
import io
import shutil
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.api.core.database import SessionLocal, DB_PATH

CATEGORY = "阀门"
PROJECT_ID = 63

# 硬编码本次参与比价的三个 submission
TARGET_SUBMISSIONS: dict[int, dict] = {
    17: {"display_name": "上海绵存", "supplier_id": None},   # 未经人工验证不关联 supplier_id
    18: {"display_name": "泰科龙",   "supplier_id": None},   # OCR 误识别为伯尔梅特，修正名称
    19: {"display_name": "凯硕新正", "supplier_id": 72},
}

# 禁止使用
BANNED_SUBMISSION_IDS = {14}

# 清理目标：仅删除该 session_id 的对齐组
CLEANUP_SESSION_ID = 17


def backup_db():
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = DB_PATH.parent / f"mempas-before-repair63-{ts}.bak"
    shutil.copy2(DB_PATH, dst)
    print(f"[backup] {dst}")
    return dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        db = SessionLocal()
        try:
            _run_verify(db)
        finally:
            db.close()
        return

    if not args.dry_run:
        backup_db()

    db = SessionLocal()
    try:
        _run(db, args.dry_run)
    finally:
        db.close()


def _run(db, dry: bool):
    from apps.api.models.bid_submission import BidSubmission, BidQuoteLine
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    from apps.api.models.tender_list_session import TenderListSession
    from apps.api.models import ExtractionJob, Supplier

    print(f"\n{'='*60}")
    print(f"project {PROJECT_ID} / category={CATEGORY} / dry={dry}")
    print(f"{'='*60}\n")

    sup_cache: dict[int, str] = {s.id: s.name for s in db.query(Supplier).all()}

    # ── 预检：列出所有 submission ────────────────────────────────────────────
    all_subs = (
        db.query(BidSubmission)
        .filter_by(project_id=PROJECT_ID)
        .order_by(BidSubmission.id)
        .all()
    )
    print("所有 submission 状态：")
    for sub in all_subs:
        cnt = db.query(BidQuoteLine).filter_by(submission_id=sub.id).count()
        sup_name = sup_cache.get(sub.supplier_id, "(unknown)") if sub.supplier_id else "(陌生)"
        job = db.get(ExtractionJob, sub.job_id)
        item_cnt = len((job.result or {}).get("items") or []) if job else 0
        ocr_name = (job.result or {}).get("supplier_name", "") if job else ""
        tag = ""
        if sub.id in BANNED_SUBMISSION_IDS:
            tag = " [!!BANNED]"
        elif sub.id in TARGET_SUBMISSIONS:
            tag = " [TARGET]"
        print(f"  sub={sub.id:3d} sup_id={sub.supplier_id} ({sup_name!r})"
              f" raw={sub.supplier_raw_name!r} ocr={ocr_name!r}"
              f" items={item_cnt} BQL={cnt} status={sub.status}{tag}")

    print()

    # ── 硬校验：当前 session id 必须 == CLEANUP_SESSION_ID ──────────────────
    _preflight_session = (
        db.query(TenderListSession)
        .filter_by(project_id=PROJECT_ID, category=CATEGORY)
        .filter(TenderListSession.is_current.is_(True))
        .first()
    )
    if _preflight_session is None or _preflight_session.id != CLEANUP_SESSION_ID:
        actual_id = _preflight_session.id if _preflight_session else None
        print(f"[!!] 当前 session id={actual_id}，预期={CLEANUP_SESSION_ID}，中止。")
        sys.exit(1)
    print(f"[preflight] session id={_preflight_session.id} [pass]")

    # ── 硬校验：filename 必须包含指定关键词 ────────────────────────────────
    _FILENAME_HINTS: dict[int, str] = {17: "绵存", 18: "泰科龙", 19: "凯硕"}
    for _chk_sub_id, _hint in _FILENAME_HINTS.items():
        _chk_sub = db.get(BidSubmission, _chk_sub_id)
        if _chk_sub is None:
            print(f"[!!] submission {_chk_sub_id} 不存在，中止。")
            sys.exit(1)
        _chk_job = db.get(ExtractionJob, _chk_sub.job_id) if _chk_sub.job_id else None
        _chk_fn = (_chk_job.filename if _chk_job else None) or ""
        if _hint not in _chk_fn:
            print(f"[!!] sub={_chk_sub_id} filename={_chk_fn!r} 不含 '{_hint}'，中止。")
            sys.exit(1)
        print(f"[preflight] sub={_chk_sub_id} filename={_chk_fn!r} 含 '{_hint}' [pass]")
    print()

    # ── 加载目标 submission 对象 ─────────────────────────────────────────────
    plan: list[dict] = []
    for sub_id, cfg in sorted(TARGET_SUBMISSIONS.items()):
        sub = db.get(BidSubmission, sub_id)
        if sub is None:
            print(f"[!!] submission {sub_id} 不存在，中止。")
            sys.exit(1)
        if sub.project_id != PROJECT_ID:
            print(f"[!!] submission {sub_id} project_id={sub.project_id}!={PROJECT_ID}，中止。")
            sys.exit(1)
        job = db.get(ExtractionJob, sub.job_id)
        item_cnt = len((job.result or {}).get("items") or []) if job else 0
        bql_cnt = db.query(BidQuoteLine).filter_by(submission_id=sub_id).count()
        plan.append({
            "sub": sub,
            "sub_id": sub_id,
            "display_name": cfg["display_name"],
            "supplier_id": cfg["supplier_id"],
            "item_cnt": item_cnt,
            "bql_before": bql_cnt,
        })

    print("修复计划（Phase 1：重建 BQL，单事务）：")
    for p in plan:
        print(f"  sub={p['sub_id']:3d} → display={p['display_name']!r}"
              f" supplier_id={p['supplier_id']}"
              f" items={p['item_cnt']} bql_before={p['bql_before']}")

    # 将被标为 superseded 的 submission
    to_supersede = [s for s in all_subs if s.id not in TARGET_SUBMISSIONS and s.status not in ("superseded", "rejected")]
    print(f"\n其他 submission 将标记 superseded：{[s.id for s in to_supersede]}")

    # 清理目标
    groups_to_delete = (
        db.query(BidAlignmentGroup)
        .filter(
            BidAlignmentGroup.project_id == PROJECT_ID,
            BidAlignmentGroup.category == CATEGORY,
            BidAlignmentGroup.tender_list_session_id == CLEANUP_SESSION_ID,
        )
        .all()
    )
    print(f"\n清理计划（Phase 3，全部校验通过后执行）：")
    print(f"  删除 tender_list_session_id={CLEANUP_SESSION_ID} 的对齐组：{len(groups_to_delete)} 个")
    if groups_to_delete:
        for g in groups_to_delete[:3]:
            print(f"    group_id={g.id} anchor_seq={g.anchor_seq} status={g.status}")
        if len(groups_to_delete) > 3:
            print(f"    ... 共 {len(groups_to_delete)} 个")

    session = (
        db.query(TenderListSession)
        .filter_by(project_id=PROJECT_ID, category=CATEGORY)
        .filter(TenderListSession.is_current.is_(True))
        .first()
    )
    print(f"\n当前 TenderListSession: id={session.id if session else 'None'}"
          f" used_submission_ids={session.used_submission_ids if session else 'None'}")

    if dry:
        print("\n[dry-run] 预检完毕，未修改任何数据。")
        print("确认计划无误后去掉 --dry-run 执行真正修复。")
        return

    # ── Phase 1+2+3：单事务原子执行 ────────────────────────────────────────
    from apps.api.services.rebuild_submission_lines import rebuild_submission_lines

    try:
        # Phase 1: 重建三家 BQL（均不提交）
        print(f"\n{'─'*40}")
        print("Phase 1：重建 BQL（事务开始）")
        print(f"{'─'*40}")

        rebuild_results: list[dict] = []
        for p in plan:
            try:
                result = rebuild_submission_lines(
                    db,
                    submission_id=p["sub_id"],
                    display_name=p["display_name"],
                    category=CATEGORY,
                    supplier_id=p["supplier_id"],
                )
                print(f"  [built] sub={p['sub_id']} ({p['display_name']!r}):"
                      f" BQL={result['line_count']} skipped={result['skipped_count']}"
                      + (f" errors={result['errors'][:1]}" if result['errors'] else ""))
                rebuild_results.append({**p, **result, "ok": True})
            except (ValueError, RuntimeError) as e:
                print(f"  [ERR] sub={p['sub_id']} ({p['display_name']!r}): {e}")
                rebuild_results.append({**p, "line_count": 0, "ok": False, "error": str(e)})

        # Phase 2: 内存校验（从 session 读 BQL 行，不需要 commit）
        print(f"\n{'─'*40}")
        print("Phase 2：校验")
        print(f"{'─'*40}")

        from apps.api.models.bid_submission import BidQuoteLine as _BQL2

        all_pass = True
        for r in rebuild_results:
            # db.flush() 已在 rebuild 内执行，可直接 count
            bql_rows = db.query(_BQL2).filter_by(submission_id=r["sub_id"]).all()
            bql_cnt = len(bql_rows)
            sub = db.get(BidSubmission, r["sub_id"])

            checks = []
            ok = True

            if bql_cnt > 0:
                checks.append(f"BQL={bql_cnt} [pass]")
            else:
                checks.append(f"BQL=0 [FAIL]")
                ok = False

            wrong_cat = [b for b in bql_rows if b.category != CATEGORY]
            if not wrong_cat:
                checks.append(f"category='{CATEGORY}' [pass]")
            else:
                checks.append(f"category 错误 {len(wrong_cat)} 行 [FAIL]")
                ok = False

            if sub.supplier_raw_name == r["display_name"]:
                checks.append(f"raw_name='{sub.supplier_raw_name}' [pass]")
            else:
                checks.append(f"raw_name='{sub.supplier_raw_name}'!='{r['display_name']}' [FAIL]")
                ok = False

            # supplier_id 精确断言
            exp_sid = r["supplier_id"]
            if sub.supplier_id == exp_sid:
                checks.append(f"supplier_id={sub.supplier_id} [pass]")
            else:
                checks.append(f"supplier_id={sub.supplier_id}!={exp_sid} [FAIL]")
                ok = False

            print(f"  sub={r['sub_id']:3d} {r['display_name']!r}: {'[pass]' if ok else '[FAIL]'}")
            for c in checks:
                print(f"    {c}")

            if not ok:
                all_pass = False

        if not all_pass:
            raise RuntimeError("Phase 2 校验失败，事务回滚")

        print("\nPhase 2 全部通过。")

        # Phase 3: 清理 + 标记 superseded + 重置 session（仍在同一事务）
        print(f"\n{'─'*40}")
        print(f"Phase 3：清理（tender_list_session_id={CLEANUP_SESSION_ID}）")
        print(f"{'─'*40}")

        gids = [g.id for g in groups_to_delete]
        if gids:
            di = db.query(BidAlignmentItem).filter(
                BidAlignmentItem.group_id.in_(gids)
            ).delete(synchronize_session=False)
            dg = db.query(BidAlignmentGroup).filter(
                BidAlignmentGroup.id.in_(gids)
            ).delete(synchronize_session=False)
            print(f"  [cleanup] 删除 {dg} 个对齐组，{di} 个对齐项")
        else:
            print("  [skip] 无需清理对齐组")

        # 标记其他 submission 为 superseded（防止 bid-matrix gate 扫描到）
        for sub in to_supersede:
            sub.status = "superseded"
            db.add(sub)
        if to_supersede:
            print(f"  [supersede] 标记 {[s.id for s in to_supersede]} → superseded")

        if session:
            session.used_submission_ids = None
            db.add(session)
            print(f"  [reset] session.id={session.id} used_submission_ids → null")

        # ── 提交整个事务 ────────────────────────────────────────────────────
        db.commit()
        print("\n[commit] 事务提交成功。")

    except Exception as e:
        db.rollback()
        print(f"\n[rollback] 事务已回滚：{e}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("修复完成。后续步骤：")
    print("  1. 在前端 Step 3「对齐核查」重新执行对齐（import_and_match）")
    print("  2. 在前端 Step 4「生成矩阵」验证矩阵结果")
    print(f"  3. 运行  .venv\\Scripts\\python.exe -X utf8 scripts\\repair_project63.py --verify  断言结果")
    print(f"{'='*60}\n")


def _run_verify(db):
    """--verify：match 后断言（需先在前端完成 Step 3 对齐）"""
    from apps.api.models.bid_submission import BidSubmission, BidQuoteLine
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    from apps.api.models.tender_list_session import TenderListSession

    print(f"\n{'='*60}")
    print(f"post-match 断言 / project={PROJECT_ID} category={CATEGORY}")
    print(f"{'='*60}\n")

    expected_sids = sorted(TARGET_SUBMISSIONS.keys())  # [17, 18, 19]

    session = (
        db.query(TenderListSession)
        .filter_by(project_id=PROJECT_ID, category=CATEGORY)
        .filter(TenderListSession.is_current.is_(True))
        .first()
    )
    if not session:
        print("[!!] 未找到 TenderListSession，无法断言。")
        sys.exit(1)

    failures: list[str] = []

    # 断言 1：used_submission_ids 精确等于 [17, 18, 19]
    actual_sids = sorted(session.used_submission_ids or [])
    if actual_sids == expected_sids:
        print(f"[pass] used_submission_ids = {actual_sids}")
    else:
        msg = f"used_submission_ids = {actual_sids}，期望 {expected_sids}"
        print(f"[FAIL] {msg}")
        failures.append(msg)

    # 断言 2：所有 BidAlignmentItem 都有 submission_id 且有 bid_quote_line_id
    groups = (
        db.query(BidAlignmentGroup)
        .filter_by(project_id=PROJECT_ID, category=CATEGORY)
        .filter(BidAlignmentGroup.tender_list_session_id == session.id)
        .all()
    )
    gids = [g.id for g in groups]
    if not gids:
        msg = "未找到任何 BidAlignmentGroup（请先完成 Step 3 对齐）"
        print(f"[!!] {msg}")
        failures.append(msg)
    else:
        items = db.query(BidAlignmentItem).filter(BidAlignmentItem.group_id.in_(gids)).all()
        total = len(items)
        missing_sub = [i for i in items if not i.submission_id]
        missing_bql = [i for i in items if not i.bid_quote_line_id]
        using_quote = [i for i in items if i.quote_id is not None]

        if not missing_sub:
            print(f"[pass] 所有 {total} 个 BidAlignmentItem 均有 submission_id")
        else:
            msg = f"{len(missing_sub)}/{total} 个 BidAlignmentItem 缺少 submission_id"
            print(f"[FAIL] {msg}")
            failures.append(msg)

        if not missing_bql:
            print(f"[pass] 所有 {total} 个 BidAlignmentItem 均有 bid_quote_line_id")
        else:
            msg = f"{len(missing_bql)}/{total} 个 BidAlignmentItem 缺少 bid_quote_line_id"
            print(f"[FAIL] {msg}")
            failures.append(msg)

        if not using_quote:
            print(f"[pass] 无 BidAlignmentItem 引用 legacy quote_id")
        else:
            msg = f"{len(using_quote)}/{total} 个 BidAlignmentItem 仍有 quote_id（legacy 污染）"
            print(f"[FAIL] {msg}")
            failures.append(msg)

    print()
    if failures:
        print(f"[!!] 断言失败 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("[OK] 所有断言通过。比价数据修复验证完成。")


if __name__ == "__main__":
    main()
