"""一次性数据清理脚本（非生产能力，默认 dry-run）— CLAUDE.md §10/§12。

目标（用户 2026-06-22 授权）：
  - 删除测试项目 proj 60「测试项目」、proj 61「测试项目1」（含全部关联数据 + 项目记录）。
  - 清空 proj 59「金桥地铁上盖」全部业务数据（含招标采购清单/锚点），仅保留项目壳。
  - 不动：quotes 历史价、materials/suppliers 主数据、其余 50+ 项目、users。

用法（在容器内 /app/data/ 下运行，DB 路径默认 /app/data/mempas.db）：
  python _wipe.py            # dry-run，仅打印逐表计划与守恒报告
  python _wipe.py --execute  # 单事务执行，断言失败则 rollback
"""
from __future__ import annotations

import sqlite3
import sys

DB_PATH = "/app/data/mempas.db"
DELETE_PROJECTS = [60, 61]          # 删除项目记录 + 数据
CLEAR_PROJECTS = [59]               # 仅清数据，保留项目记录
ALL_PROJECTS = DELETE_PROJECTS + CLEAR_PROJECTS

EXPECTED_NAMES = {59: "金桥地铁上盖", 60: "测试项目", 61: "测试项目1"}


def main() -> None:
    execute = "--execute" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── 断言：项目名必须与预期一致（防误删）──
    for pid, exp in EXPECTED_NAMES.items():
        row = cur.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise SystemExit(f"ABORT: project {pid} 不存在")
        if row["name"] != exp:
            raise SystemExit(f"ABORT: project {pid} 名='{row['name']}' ≠ 预期'{exp}'")
    print(f"[assert] 项目名校验通过: {EXPECTED_NAMES}")

    ph = ",".join("?" * len(ALL_PROJECTS))

    # ── 收集关联 ID ──
    submission_ids = [r[0] for r in cur.execute(
        f"SELECT id FROM bid_submissions WHERE project_id IN ({ph})", ALL_PROJECTS).fetchall()]
    group_ids = [r[0] for r in cur.execute(
        f"SELECT id FROM bid_alignment_groups WHERE project_id IN ({ph})", ALL_PROJECTS).fetchall()]
    job_ids = set(r[0] for r in cur.execute(
        f"SELECT job_id FROM bid_submissions WHERE project_id IN ({ph}) AND job_id IS NOT NULL", ALL_PROJECTS).fetchall())
    job_ids |= set(r[0] for r in cur.execute(
        f"SELECT job_id FROM tender_documents WHERE project_id IN ({ph}) AND job_id IS NOT NULL", ALL_PROJECTS).fetchall())

    print(f"[collect] submissions={len(submission_ids)} groups={len(group_ids)} jobs={len(job_ids)}")

    sub_ph = ",".join("?" * len(submission_ids)) if submission_ids else "NULL"
    grp_ph = ",".join("?" * len(group_ids)) if group_ids else "NULL"

    # ── 逐表删除计划（子表先于父表）──
    plan = []  # (label, sql, params)
    if submission_ids:
        plan.append(("bid_quote_lines",
                     f"DELETE FROM bid_quote_lines WHERE submission_id IN ({sub_ph})", submission_ids))
    # bid_alignment_items: 按 group_id 或 submission_id（两路径都覆盖）
    ai_clauses, ai_params = [], []
    if group_ids:
        ai_clauses.append(f"group_id IN ({grp_ph})"); ai_params += group_ids
    if submission_ids:
        ai_clauses.append(f"submission_id IN ({sub_ph})"); ai_params += submission_ids
    if ai_clauses:
        plan.append(("bid_alignment_items",
                     f"DELETE FROM bid_alignment_items WHERE {' OR '.join(ai_clauses)}", ai_params))
    plan.append(("bid_alignment_groups",
                 f"DELETE FROM bid_alignment_groups WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("bid_matrix_versions",
                 f"DELETE FROM bid_matrix_versions WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("alignment_finalizations",
                 f"DELETE FROM alignment_finalizations WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("tender_documents",
                 f"DELETE FROM tender_documents WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("bid_submissions",
                 f"DELETE FROM bid_submissions WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("tender_list_sessions",
                 f"DELETE FROM tender_list_sessions WHERE project_id IN ({ph})", ALL_PROJECTS))
    plan.append(("quotes(应为0)",
                 f"DELETE FROM quotes WHERE project_id IN ({ph})", ALL_PROJECTS))

    # ── dry-run 计数 ──
    print("\n=== 删除计划（影响行数）===")
    for label, sql, params in plan:
        cnt_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
        n = cur.execute(cnt_sql, params).fetchone()[0]
        print(f"  {label:28s} -> {n} 行")

    # extraction_jobs: 仅删除孤儿（不被任何"存活"submission/tender_document/quote.batch_id 引用）
    orphan_jobs = []
    for jid in job_ids:
        refs = cur.execute(
            f"SELECT (SELECT COUNT(*) FROM bid_submissions WHERE job_id=? AND project_id NOT IN ({ph}))"
            f" + (SELECT COUNT(*) FROM tender_documents WHERE job_id=? AND project_id NOT IN ({ph}))"
            f" + (SELECT COUNT(*) FROM quotes WHERE batch_id=?)",
            [jid] + ALL_PROJECTS + [jid] + ALL_PROJECTS + [jid]).fetchone()[0]
        if refs == 0:
            orphan_jobs.append(jid)
    print(f"  extraction_jobs(孤儿)        -> {len(orphan_jobs)} / {len(job_ids)} 个")

    print(f"\n=== 项目记录 ===")
    print(f"  删除 projects: {DELETE_PROJECTS}")
    print(f"  保留 projects(清空数据): {CLEAR_PROJECTS}")

    if not execute:
        print("\n[DRY-RUN] 未修改任何数据。加 --execute 执行。")
        conn.close()
        return

    # ── 执行（单事务 + 断言）──
    print("\n[EXECUTE] 开始单事务删除...")
    try:
        for label, sql, params in plan:
            cur.execute(sql, params)
        if orphan_jobs:
            jph = ",".join("?" * len(orphan_jobs))
            cur.execute(f"DELETE FROM extraction_jobs WHERE id IN ({jph})", orphan_jobs)
        cur.execute(f"DELETE FROM projects WHERE id IN ({','.join('?'*len(DELETE_PROJECTS))})", DELETE_PROJECTS)

        # ── 守恒断言 ──
        for pid in ALL_PROJECTS:
            for tbl, col in [("bid_submissions", "project_id"),
                             ("bid_alignment_groups", "project_id"),
                             ("tender_list_sessions", "project_id"),
                             ("bid_matrix_versions", "project_id")]:
                left = cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (pid,)).fetchone()[0]
                assert left == 0, f"断言失败: {tbl}.{col}={pid} 仍剩 {left} 行"
        # bql 经 submission 间接清零
        left_bql = cur.execute(
            "SELECT COUNT(*) FROM bid_quote_lines WHERE submission_id IN "
            f"(SELECT id FROM bid_submissions WHERE project_id IN ({ph}))", ALL_PROJECTS).fetchone()[0]
        assert left_bql == 0, f"断言失败: 残留 bql {left_bql}"
        # 项目记录
        for pid in DELETE_PROJECTS:
            assert cur.execute("SELECT COUNT(*) FROM projects WHERE id=?", (pid,)).fetchone()[0] == 0, \
                f"断言失败: 项目 {pid} 未删除"
        for pid in CLEAR_PROJECTS:
            assert cur.execute("SELECT COUNT(*) FROM projects WHERE id=?", (pid,)).fetchone()[0] == 1, \
                f"断言失败: 项目 {pid} 壳丢失"
        # 守恒：历史价/主数据不动
        q_total = cur.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        m_total = cur.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        s_total = cur.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
        p_total = cur.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        conn.commit()
        print("[OK] 提交成功。守恒报告:")
        print(f"  quotes(历史价)={q_total}  materials={m_total}  suppliers={s_total}  projects={p_total}")
    except Exception as e:
        conn.rollback()
        print(f"[ROLLBACK] 断言/执行失败，已回滚: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
