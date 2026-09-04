"""把历史价格基准导入目标库（默认 dry-run）。

只导入**主数据 / 历史价格**，绝不携带比价流程数据（BidSubmission /
BidQuoteLine / 对齐 / 轮次 / 识别任务）——这正是 CLAUDE.md §4「暂存不污染
主数据」隔离不变式的另一半：反向也不许把流程数据混进基准。

按**列名**逐列插入，不用位置插入：本地库与生产库的列集合相同但**顺序不同**
（2026-09-03 实测），位置插入会静默错列，把价格写进数量。

用法：
  python scripts/import_history_baseline.py --db /app/data/mempas.db --src /tmp/history_export.db
  python scripts/import_history_baseline.py --db ... --src ... --apply
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

# 导入顺序 = FK 依赖顺序：被引用的先进。
TABLES = ["projects", "materials", "suppliers", "supplier_aliases", "brand_tiers", "quotes"]

# 明确不得出现在导入源里的表——出现即中止。
FORBIDDEN = [
    "bid_submissions", "bid_quote_lines", "bid_alignment_groups",
    "bid_alignment_items", "tender_list_sessions", "quote_rounds", "extraction_jobs",
]


def cols(con, schema: str, table: str) -> list[str]:
    """附加库的列要写成 `PRAGMA <schema>.table_info(<table>)`。

    写成 `PRAGMA table_info(src.t)` 是语法错误——点号不能出现在参数里。
    """
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="目标库")
    ap.add_argument("--src", required=True, help="导出库")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not Path(a.src).is_file():
        sys.exit(f"找不到导出库：{a.src}")

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS src", (a.src,))

    # 闸门：导出库里不许有流程数据
    src_tables = {r[0] for r in con.execute("SELECT name FROM src.sqlite_master WHERE type='table'")}
    bad = [t for t in FORBIDDEN if t in src_tables]
    if bad:
        sys.exit(f"中止：导出库含比价流程表 {bad}——基准导入不得携带流程数据")

    print(f"{'APPLY' if a.apply else 'DRY-RUN'} — 目标={a.db} 源={a.src}\n")
    print("-- 计划 --")
    plan = {}
    for t in TABLES:
        if t not in src_tables:
            print(f"  {t:20} 源中无此表，跳过")
            continue
        n_src = con.execute(f"SELECT COUNT(*) FROM src.{t}").fetchone()[0]
        n_dst = con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        c_src, c_dst = cols(con, "src", t), cols(con, "main", t)
        shared = [c for c in c_src if c in c_dst]
        missing = set(c_src) ^ set(c_dst)
        plan[t] = shared
        note = f"  列差异={sorted(missing)}" if missing else ""
        print(f"  {t:20} 源 {n_src:>6} 行 → 目标现有 {n_dst:>6} 行, 按 {len(shared)} 个同名列插入{note}")

    if not a.apply:
        print("\n(dry-run，未写入；确认后加 --apply)")
        return

    before = {t: con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0] for t in plan}
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = Path(a.db).with_name(Path(a.db).stem + f".before-history-import-{ts}.db")
    out = sqlite3.connect(str(bak))
    con.backup(out)
    out.close()
    print(f"\n备份: {bak}")

    con.execute("BEGIN")
    try:
        for t, shared in plan.items():
            q = ",".join(f'"{c}"' for c in shared)
            # INSERT OR IGNORE：主键冲突时跳过而不是覆盖，避免二次运行把目标
            # 库已有的行改写掉（幂等）。
            con.execute(f"INSERT OR IGNORE INTO main.{t} ({q}) SELECT {q} FROM src.{t}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    print("\n-- 守恒报告 (before → after) --")
    ok = True
    for t in plan:
        aft = con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        src_n = con.execute(f"SELECT COUNT(*) FROM src.{t}").fetchone()[0]
        exp = before[t] + src_n
        mark = "OK" if aft == exp else f"!! 期望 {exp}"
        ok = ok and aft == exp
        print(f"  {t:20} {before[t]:>6} → {aft:>6}   (源 {src_n})  {mark}")

    print("\n-- 引用完整性 --")
    checks = [
        ("quotes.material_id 悬空",
         "SELECT COUNT(*) FROM main.quotes WHERE material_id IS NOT NULL "
         "AND material_id NOT IN (SELECT id FROM main.materials)"),
        ("quotes.project_id 悬空",
         "SELECT COUNT(*) FROM main.quotes WHERE project_id IS NOT NULL "
         "AND project_id NOT IN (SELECT id FROM main.projects)"),
        ("quotes.supplier_id 悬空",
         "SELECT COUNT(*) FROM main.quotes WHERE supplier_id IS NOT NULL "
         "AND supplier_id NOT IN (SELECT id FROM main.suppliers)"),
    ]
    for label, sql in checks:
        n = con.execute(sql).fetchone()[0]
        print(f"  {label:28} {n:>6}   {'OK' if n == 0 else '!! 悬空'}")
        ok = ok and n == 0

    print("\n-- 流程表必须仍为空 --")
    for t in FORBIDDEN:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        except sqlite3.Error:
            continue
        print(f"  {t:24} {n:>6}   {'OK' if n == 0 else '!! 被污染'}")
        ok = ok and n == 0

    con.commit()
    con.close()
    print("\n结果:", "OK" if ok else "有问题，请从备份恢复")


if __name__ == "__main__":
    main()
