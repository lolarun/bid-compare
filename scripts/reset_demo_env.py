"""重置比价演示环境：清空比价流程数据，保留历史价格基准，建好指定项目。

只读为默认，`--apply` 才写。遵循 .claude/rules/database-safety.md：
先备份 → dry-run → 输出前后守恒报告 → 人工确认。

**保留什么、为什么**
  历史价格（`quotes`/`materials`/`suppliers`/`brand_tiers`）是「偏差分析」和
  「历史价格查询」的基准数据源，清掉它们客户点开那两个功能全是空的，比价也
  没有参考价基准。默认保留。
  连带保留**携带历史报价的项目行**：仪表盘热力图按 `Project.name` 分组
  （`statistics.get_dashboard_heatmap`），删掉项目行热力图就空了——那与"保留
  历史价格"自相矛盾。这些项目没有轮次/报价，`/projects/overview` 的
  `include_empty=False` 本来就会把它们挡在比价入口列表外。
  用 `--purge-history` / `--purge-history-projects` 可以推翻这两个默认。

用法：
  python scripts/reset_demo_env.py --db data/mempas.db                 # dry-run
  python scripts/reset_demo_env.py --db data/mempas.db --apply \
      --uploads data/uploads --create "徐汇区华泾镇项目,金桥地体上盖项目,临港中科院项目"
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

# 比价流程数据，按 FK 方向从叶子往根删。
FLOW_TABLES = [
    ("bid_alignment_items", "DELETE FROM bid_alignment_items"),
    ("bid_alignment_groups", "DELETE FROM bid_alignment_groups"),
    ("bid_quote_lines", "DELETE FROM bid_quote_lines"),
    ("bid_submissions", "DELETE FROM bid_submissions"),
    ("tender_list_sessions", "DELETE FROM tender_list_sessions"),
    ("quote_rounds", "DELETE FROM quote_rounds"),
    ("extraction_jobs", "DELETE FROM extraction_jobs"),
    ("tender_documents", "DELETE FROM tender_documents"),
    ("bid_matrix_versions", "DELETE FROM bid_matrix_versions"),
    ("alignment_finalizations", "DELETE FROM alignment_finalizations"),
    ("anchor_missing_acks", "DELETE FROM anchor_missing_acks"),
    ("bid_invitations", "DELETE FROM bid_invitations"),
]

# 历史价格与主数据——默认一律不动。
HISTORY_TABLES = ["quotes", "materials", "suppliers", "supplier_aliases", "brand_tiers"]

REPORT_TABLES = [
    "projects", "quotes", "materials", "suppliers", "brand_tiers",
    "bid_submissions", "bid_quote_lines", "bid_alignment_groups",
    "bid_alignment_items", "tender_list_sessions", "quote_rounds",
    "extraction_jobs", "tender_documents", "users",
]


def table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def counts(con) -> dict:
    out = {}
    for t in REPORT_TABLES:
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if table_exists(con, t) else None
    return out


def history_project_ids(con) -> set[int]:
    """挂着历史报价的项目 id——热力图按项目分组，删了它就空了。"""
    if not table_exists(con, "quotes"):
        return set()
    return {
        r[0] for r in con.execute(
            "SELECT DISTINCT project_id FROM quotes WHERE project_id IS NOT NULL"
        )
    }


def backup(db: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = db.parent / f"{db.stem}.before-reset-{ts}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)          # WAL 安全；文件拷贝会丢最近提交
    out.close(); src.close()
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--uploads", default=None, help="上传文件目录，给了才清")
    ap.add_argument("--create", default="", help="逗号分隔的项目名，重置后创建")
    ap.add_argument("--purge-history", action="store_true",
                    help="连历史价格/物料/供应商一起清空（默认保留）")
    ap.add_argument("--purge-history-projects", action="store_true",
                    help="连携带历史报价的项目行也删（默认保留，否则热力图会空）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.is_file():
        sys.exit(f"找不到数据库：{db}")

    con = sqlite3.connect(str(db) if a.apply else f"file:{db}?mode=ro", uri=not a.apply)
    before = counts(con)
    keep_pids = set() if a.purge_history_projects else history_project_ids(con)
    total_projects = before["projects"] or 0
    to_delete_projects = total_projects - len(keep_pids)

    print(f"{'APPLY' if a.apply else 'DRY-RUN'} — db={db}")
    print("\n-- 计划 --")
    print(f"  比价流程表           : 全部清空（{len(FLOW_TABLES)} 张）")
    print(f"  历史价格/主数据      : {'一并清空' if a.purge_history else '保留'}"
          f"（{', '.join(HISTORY_TABLES)}）")
    print(f"  项目                 : 删除 {to_delete_projects} / {total_projects}"
          f"，保留 {len(keep_pids)} 个（携带历史报价：{sorted(keep_pids) or '无'}）")
    if a.create:
        print(f"  新建项目             : {a.create}")
    if a.uploads:
        up = Path(a.uploads)
        n = sum(1 for _ in up.rglob('*') if _.is_file()) if up.is_dir() else 0
        size = sum(f.stat().st_size for f in up.rglob('*') if f.is_file()) if up.is_dir() else 0
        print(f"  上传文件             : 清空 {up}（{n} 个文件, {size/1048576:.1f} MB）")

    if not a.apply:
        print("\n(dry-run，未写入；确认无误后加 --apply)")
        return

    bak = backup(db)
    print(f"\n备份: {bak} ({bak.stat().st_size/1048576:.1f} MB)")

    con.execute("BEGIN")
    try:
        for name, sql in FLOW_TABLES:
            if table_exists(con, name):
                con.execute(sql)
        if a.purge_history:
            for t in HISTORY_TABLES:
                if table_exists(con, t):
                    con.execute(f"DELETE FROM {t}")
        if keep_pids:
            ph = ",".join("?" * len(keep_pids))
            con.execute(f"DELETE FROM projects WHERE id NOT IN ({ph})", tuple(keep_pids))
        else:
            con.execute("DELETE FROM projects")
        for name in [n.strip() for n in a.create.split(",") if n.strip()]:
            con.execute(
                "INSERT INTO projects (name, code, location, status, remark, created_at) "
                "VALUES (?, '', '', 'active', '', ?)",
                (name, datetime.datetime.now().isoformat(sep=" ")),
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    after = counts(con)
    print("\n-- 守恒报告 (before → after) --")
    for t in REPORT_TABLES:
        b, x = before[t], after[t]
        if b is None:
            continue
        mark = "" if b == x else "  <-- 变化"
        print(f"  {t:24} {b:>7} → {x:>7}{mark}")

    print("\n-- 孤儿检查 --")
    checks = [
        ("bid_quote_lines 无 submission",
         "SELECT COUNT(*) FROM bid_quote_lines WHERE submission_id NOT IN (SELECT id FROM bid_submissions)"),
        ("quotes 指向已删项目",
         "SELECT COUNT(*) FROM quotes WHERE project_id IS NOT NULL "
         "AND project_id NOT IN (SELECT id FROM projects)"),
    ]
    ok = True
    for label, sql in checks:
        try:
            n = con.execute(sql).fetchone()[0]
        except sqlite3.Error:
            continue
        print(f"  {label:34} {n:>6}   {'OK' if n == 0 else '!! 有孤儿'}")
        ok = ok and n == 0

    print("\n-- 新项目 --")
    for r in con.execute("SELECT id, name FROM projects ORDER BY id"):
        print(f"  #{r[0]:<5} {r[1]}")

    if a.uploads:
        up = Path(a.uploads)
        if up.is_dir():
            for child in up.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            print(f"\n上传目录已清空: {up}")

    con.close()
    print("\n结果:", "OK" if ok else "有孤儿，请从备份恢复")


if __name__ == "__main__":
    main()
