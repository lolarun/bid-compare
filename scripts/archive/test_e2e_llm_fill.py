"""实验 B — 同数据 A/B：embedding vs LLM 供应商填表。

主证据：同一批已入库报价 + 同一采购清单 session，先跑 /tender-list/match（纯
embedding）记 comparable_2plus，再跑 /tender-list/llm-fill（replace）记
comparable_2plus，直接量化 LLM 判断增益。

前置：后端已在 8002 跑；项目里已有 ≥2 家供应商报价 + 已确认 TenderListSession。
本脚本不重新上传报价——它复用 test_e2e_anchor / test_e2e_tabular 留下的数据，
或任何已 batch-confirm 的项目。用法：

    python scripts/test_e2e_llm_fill.py --project E2E_v24_test --category 阀门

需要 DASHSCOPE_API_KEY 才能跑真实 LLM；否则 llm-fill 会因 embedding/LLM 调用失败。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import os

import requests

API = "http://localhost:8002"
DB = os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="E2E_v24_test", help="项目名或项目ID(纯数字)")
    ap.add_argument("--category", default="阀门")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--missing-audit", action="store_true", help="打印 missing_audit 详情")
    ap.add_argument("--assert-regression", "--regression", dest="assert_regression",
                    action="store_true",
                    help="Project-62 回归断言：#28-31 OCR纠错放行 / #46/#70/#83/#84 误匹配拦截 / can_finalize / 矩阵指标")
    args = ap.parse_args()

    log("=== 实验 B：embedding vs LLM 填表 A/B ===")

    tok = requests.post(
        f"{API}/api/auth/login",
        json={"username": args.user, "password": args.password},
    ).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    # 解析 project_id + supplier_ids + session
    conn = sqlite3.connect(DB)
    if args.project.isdigit():
        prow = conn.execute("SELECT id FROM projects WHERE id=?", (int(args.project),)).fetchone()
        if not prow:
            log(f"FAIL: 未找到 project_id={args.project}")
            sys.exit(1)
    else:
        prow = conn.execute("SELECT id FROM projects WHERE name=? ORDER BY id DESC LIMIT 1",
                            (args.project,)).fetchone()
    if not prow:
        log(f"FAIL: 未找到项目 {args.project!r}，请先跑 test_e2e_anchor/test_e2e_tabular")
        sys.exit(1)
    pid = prow[0]
    sids = [r[0] for r in conn.execute(
        "SELECT DISTINCT supplier_id FROM quotes WHERE project_id=? AND supplier_id IS NOT NULL",
        (pid,)).fetchall()]
    srow = conn.execute(
        "SELECT id FROM tender_list_sessions WHERE project_id=? AND category=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1", (pid, args.category)).fetchone()
    conn.close()

    if len(sids) < 2:
        log(f"FAIL: 项目 {pid} 下供应商不足 2 家：{sids}")
        sys.exit(1)
    if not srow:
        log(f"FAIL: 项目 {pid}/{args.category} 无已确认 TenderListSession")
        sys.exit(1)
    session_id = srow[0]
    N = len(sids)
    log(f"project_id={pid}  suppliers={sids} (N={N})  session={session_id}")

    # ── A. embedding 基线 ─────────────────────────────────────────────────────
    log("跑 embedding 匹配（/tender-list/match）...")
    r = requests.post(
        f"{API}/api/analysis/tender-list/match",
        data={"project_id": str(pid), "category": args.category,
              "supplier_ids": ",".join(map(str, sids))},
        headers=H, timeout=180,
    )
    if r.status_code != 200:
        log(f"FAIL match {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    emb = r.json()
    emb_cmp2 = emb["comparable_2plus"]
    anchors_total = emb["anchors_total"]

    # ── B. LLM 填表 ───────────────────────────────────────────────────────────
    log("跑 LLM 供应商填表（/tender-list/llm-fill, replace）...")
    r2 = requests.post(
        f"{API}/api/analysis/tender-list/llm-fill",
        json={"project_id": pid, "category": args.category, "supplier_ids": sids,
              "tender_list_session_id": session_id, "mode": "replace"},
        headers=H, timeout=600,
    )
    if r2.status_code != 200:
        log(f"FAIL llm-fill {r2.status_code}: {r2.text[:500]}")
        sys.exit(1)
    llm = r2.json()
    llm_cmp2_any = llm["comparable_2plus"]
    llm_cmp2_qot = llm.get("comparable_2plus_quoted", llm_cmp2_any)
    baseline = llm["comparable_2plus_embedding_baseline"]

    # ── 提前提取 missing_audit（多处复用） ────────────────────────────────────
    ma = llm.get("missing_audit") or []
    fp_audit = llm.get("false_positive_audit", [])
    fp_align_cnt = llm.get("false_positive_align_count", 0)

    # ── C. /bid-matrix（一致性检查用，必须成功） ─────────────────────────────
    log("拉取 /bid-matrix（matrix_distribution 同源校验）...")
    r3 = requests.post(
        f"{API}/api/analysis/bid-matrix",
        json={"project_id": pid, "supplier_ids": sids, "category": args.category},
        headers=H, timeout=60,
    )
    if r3.status_code != 200:
        log(f"FAIL: /bid-matrix {r3.status_code}: {r3.text[:300]}")
        sys.exit(1)
    _bm_json = r3.json()
    bm_md: dict = _bm_json.get("matrix_distribution") or {}

    # P0-1 regression: anchor-driven matrix must be used (no legacy 449-row fallback)
    _bm_anchor = _bm_json.get("anchor_matrix")
    _bm_rows_count = len(_bm_json.get("rows") or [])
    if _bm_anchor is not True:
        log(f"FAIL: /bid-matrix anchor_matrix={_bm_anchor!r} — legacy fallback detected")
        sys.exit(1)
    if _bm_rows_count != 90:
        log(f"FAIL: /bid-matrix rows={_bm_rows_count}, expected 90 (anchor list)")
        sys.exit(1)
    log(f"OK: /bid-matrix anchor_matrix=True, rows={_bm_rows_count}")

    # ── 供应商对齐分布矩阵（从后端 matrix_distribution 读取，生产口径） ────────
    md = llm.get("matrix_distribution") or {}
    q_dist_raw = md.get("quoted_distribution") or {}
    c_dist_raw = md.get("covered_distribution") or {}
    quoted_ge_2_count  = md.get("quoted_ge_2_count", 0)    # 可比价锚点
    quoted_full_count  = md.get("quoted_full_count", 0)     # N家完整自动比价
    covered_ge_2_count = md.get("covered_ge_2_count", 0)   # covered ≥2家（复核后潜力）
    covered_full_count = md.get("covered_full_count", 0)    # N家完整覆盖（含 pending）
    # Override llm_cmp2_any with authoritative covered_ge_2 from matrix_distribution
    llm_cmp2_any = covered_ge_2_count
    # Convert string keys from JSON to int for display
    q_dist = {int(k): v for k, v in q_dist_raw.items()}
    c_dist = {int(k): v for k, v in c_dist_raw.items()}
    q_total = sum(q_dist.values())
    c_total = sum(c_dist.values())

    # ── 结果输出 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("实验 B 结果：embedding vs LLM 填表")
    print("=" * 60)
    print(f"  采购清单锚点:                    {anchors_total}")
    print(f"  供应商数 N:                      {N}")
    print(f"  embedding 可比≥2:                {emb_cmp2}/{anchors_total} = {emb_cmp2/max(anchors_total,1)*100:.1f}%")
    print(f"  embedding 基线(topk):            {baseline}/{anchors_total}")
    delta = llm_cmp2_any - emb_cmp2
    print(f"  LLM ≥2家 quoted+pending:         {llm_cmp2_any}/{anchors_total} = {llm_cmp2_any/max(anchors_total,1)*100:.1f}%"
          f"  delta={delta:+d} {'→增益' if delta > 0 else ('→持平' if delta == 0 else '→需查')}")
    print(f"  finalization 失效:               {llm['finalization_invalidated']}")

    print("\n  每家填表明细:")
    print(f"  {'供应商':<16} {'quoted':>6} {'agg':>4} {'pend':>5} {'excl':>5} {'res':>4} {'res↑':>5} {'drop':>5}")
    print("  " + "-" * 60)
    for f in llm["per_supplier_fill"]:
        print(f"  {f['supplier_name'][:14]:<16} {f['quoted']:>6} {f['aggregated']:>4} "
              f"{f['pending']:>5} {f['excluded']:>5} {f['residue']:>4} "
              f"{f['residue_high_cos']:>5} {f['dropped']:>5}"
              + (f"  ERROR: {f['error']}" if f.get("error") else ""))

    # ── 矩阵分布输出 ──────────────────────────────────────────────────────────
    print(f"\n  供应商对齐分布（quoted-only）:")
    for k in range(N + 1):
        bar = "█" * q_dist[k] if q_dist[k] <= 20 else "█" * 20 + f"…+{q_dist[k]-20}"
        print(f"    {k}/{N} 家: {q_dist[k]:>3} 个锚点  {bar}")
    if q_total != anchors_total:
        print(f"  [WARN] quoted_distribution 合计={q_total} ≠ anchors_total={anchors_total}"
              " (missing_audit 可能被截断)")

    print(f"\n  供应商覆盖分布（quoted+pending）:")
    for k in range(N + 1):
        bar = "█" * c_dist[k] if c_dist[k] <= 20 else "█" * 20 + f"…+{c_dist[k]-20}"
        print(f"    {k}/{N} 家: {c_dist[k]:>3} 个锚点  {bar}")
    if c_total != anchors_total:
        print(f"  [WARN] covered_distribution 合计={c_total} ≠ anchors_total={anchors_total}")

    print(f"\n  派生指标:")
    print(f"    可比价锚点（quoted ≥2家）:              {quoted_ge_2_count}/{anchors_total}"
          f" = {quoted_ge_2_count/max(anchors_total,1)*100:.1f}%")
    print(f"    {N}家完整 quoted:                       {quoted_full_count}/{anchors_total}"
          f" = {quoted_full_count/max(anchors_total,1)*100:.1f}%")
    print(f"    复核后潜在{N}家完整（covered={N}/N）:   {covered_full_count}/{anchors_total}"
          f" = {covered_full_count/max(anchors_total,1)*100:.1f}%")

    if llm.get("dropped_audit"):
        non_fp_drops = [d for d in llm["dropped_audit"] if d.get("reason") != "valve_type_conflict"]
        print(f"\n  丢弃审计(前 10，不含 valve_type_conflict)：")
        for d in non_fp_drops[:10]:
            print(f"    supplier={d.get('supplier_id')} quote={d.get('quote_id')} "
                  f"anchor={d.get('anchor_seq')} reason={d.get('reason')}")

    # ── False positive audit ──────────────────────────────────────────────────
    print(f"\n  ┌─ False Positive 审计（阀型冲突强降级）────────────────────┐")
    print(f"  │  降级条数: {len(fp_audit):>4}   "
          f"剩余quoted中误匹配: {fp_align_cnt:>3}                     │")
    if fp_audit:
        print(f"  │  {'anchor':>4}  {'supplier':<14} {'qid':>6}  {'冲突':^20}  {'quote_text':<30} │")
        print(f"  │  " + "-" * 72 + " │")
        for fp in fp_audit[:20]:
            conflict = f"{fp.get('anchor_vt','?')} != {fp.get('quote_vt','?')}"
            txt = (fp.get("quote_text") or "")[:28]
            sname = fp.get("supplier_name", str(fp.get("supplier_id", "?")))[:12]
            print(f"  │  #{fp.get('anchor_seq','?'):>3}  {sname:<14} {fp.get('quote_id','?'):>6}  {conflict:^20}  {txt:<30} │")
    print(f"  └──────────────────────────────────────────────────────────────┘")

    if args.missing_audit or ma:
        print(f"\n  缺配 / 嫌疑锚点审计（共 {len(ma)} 条，is_suspect=True 优先显示）：")
        for entry in ma:
            seq = entry["anchor_seq"]
            name = entry["anchor_name"]
            spec = entry.get("anchor_spec", "")
            qcnt = entry["quoted_count"]
            suspect = "★" if entry.get("is_suspect") else " "
            print(f"\n  {suspect}#{seq:>3} {name} {spec} — 已报价家数: {qcnt}")
            for sup in entry.get("suppliers", []):
                sname = sup["supplier_name"][:12]
                status = sup.get("status", "missing")
                conf = sup.get("confidence", 0.0)
                flags = ",".join(sup.get("flags") or []) or "-"
                print(f"          {sname:<14} {status:<12} conf={conf:.2f} flags={flags}")
                for nc in (sup.get("nearest_quote_candidates") or [])[:2]:
                    qid = nc.get("quote_id", "?")
                    txt = nc.get("text") or nc.get("material") or nc.get("why_rejected") or ""
                    why = nc.get("why_rejected", "")
                    print(f"            -> qid={qid}: {txt[:40]}  rejected={why[:40]}")

    # c5f computed here (before regression block) so R5 can reference it
    c5f = bool(bm_md) and bm_md == md

    # ── 回归断言 Project-62 ───────────────────────────────────────────────────
    reg_failures: list[str] = []
    if args.assert_regression:
        fp_seqs = {e.get("anchor_seq") for e in fp_audit}

        # Build cell_map from missing_audit
        cell_map: dict[tuple, dict] = {}
        for entry in ma:
            seq = entry["anchor_seq"]
            for sup in entry.get("suppliers", []):
                cell_map[(seq, sup["supplier_name"])] = sup

        print("\n  ── 回归断言 (Project-62) ──────────────────────────────────────")

        # R1: #28-31 凯硕新正 must be quoted with ocr_corrected_verified
        for anchor_seq in [28, 29, 30, 31]:
            sup_key = None
            for (s, n), v in cell_map.items():
                if s == anchor_seq and "凯硕" in n:
                    sup_key = (s, n); break
            if sup_key is None:
                reg_failures.append(f"  #{anchor_seq} 凯硕新正 未出现在 missing_audit")
                continue
            cell = cell_map[sup_key]
            status = cell.get("status", "")
            flags = cell.get("flags") or []
            ok = status == "quoted" and "ocr_corrected_verified" in flags
            tag = "OK  " if ok else "FAIL"
            print(f"  [{tag}] #{anchor_seq} 凯硕新正 status={status} flags={flags}")
            if not ok:
                reg_failures.append(f"  #{anchor_seq} 凯硕 expected quoted+ocr_corrected_verified, got status={status} flags={flags}")

        # R2: cross-type conflicts intercepted when LLM makes the wrong assignment.
        # All three anchors are non-deterministic: LLM sometimes skips the wrong assignment
        # entirely (no conflict fires, but also no wrong match accepted) — WARN only.
        for anchor_seq in [83, 84, 46]:
            ok = anchor_seq in fp_seqs
            print(f"  [{'OK  ' if ok else 'WARN'}] #{anchor_seq} 在 false_positive_audit (流量测试，非确定性) = {ok}")

        # R2b: 减压阀族 false-kill recovery
        for anchor_seq in [70, 71, 72, 73, 74]:
            bad = anchor_seq in fp_seqs
            tag = "FAIL" if bad else "OK  "
            print(f"  [{tag}] #{anchor_seq} 减压阀族未被误降级 (不在 false_positive_audit) = {not bad}")
            if bad:
                reg_failures.append(f"  #{anchor_seq} 减压阀族被误判为阀型冲突（family 归一失效）")

        # R2b-matrix: quoted_ge_2 >= 74 (regression floor; P0 fix baseline 76-77,
        # -3 margin for LLM non-determinism)
        _r2b_ok = quoted_ge_2_count >= 74
        tag = "OK  " if _r2b_ok else "FAIL"
        print(f"  [{tag}] quoted >=2家 >= 74/90 (减压阀族恢复): 实际={quoted_ge_2_count}/{anchors_total}")
        if not _r2b_ok:
            reg_failures.append(f"  quoted_ge_2={quoted_ge_2_count} < 74，减压阀族恢复不足")

        # R3: readiness.can_finalize must be True
        readiness = llm.get("readiness") or {}
        can_fin = readiness.get("can_finalize", True)
        tag = "OK  " if can_fin else "FAIL"
        print(f"  [{tag}] readiness.can_finalize = {can_fin}  warnings={readiness.get('warnings', [])}")
        if not can_fin:
            reg_failures.append(f"  can_finalize=False warnings={readiness.get('warnings', [])}")

        # R4: matrix distribution baselines (Project-62 specific, DB-based from build_anchor_matrix)
        # quoted_full_count: ~25-30/90 (3/3 quoted) — floor=22 for LLM variance
        # covered_full_count: ~48/90 (3/3 covered) — floor=38; old 85 was based on
        #   incorrect in-memory inference that assumed all non-audit anchors were 3/3 covered
        _R4_QUOTED_FULL_MIN = 22
        _R4_COVERED_FULL_MIN = 38
        _r4a_ok = quoted_full_count >= _R4_QUOTED_FULL_MIN
        tag = "OK  " if _r4a_ok else "FAIL"
        print(f"  [{tag}] {N}/{N}家 quoted_full >= {_R4_QUOTED_FULL_MIN}/90: 实际={quoted_full_count}")
        if not _r4a_ok:
            reg_failures.append(f"  quoted_full_count={quoted_full_count} < {_R4_QUOTED_FULL_MIN}")
        _r4b_ok = covered_full_count >= _R4_COVERED_FULL_MIN
        tag = "OK  " if _r4b_ok else "FAIL"
        print(f"  [{tag}] {N}/{N}家 covered_full >= {_R4_COVERED_FULL_MIN}/90: 实际={covered_full_count}")
        if not _r4b_ok:
            reg_failures.append(f"  covered_full_count={covered_full_count} < {_R4_COVERED_FULL_MIN}")

        # R5: /llm-fill == /bid-matrix matrix_distribution — always FAIL (no skip)
        _r5_ok = c5f  # reuse C5f result; /bid-matrix call already enforced above
        tag = "OK  " if _r5_ok else "FAIL"
        print(f"  [{tag}] /llm-fill == /bid-matrix matrix_distribution（同源硬断言）")
        if not _r5_ok:
            reg_failures.append("  matrix_distribution: /llm-fill != /bid-matrix（投产设计违反）")

        if reg_failures:
            print("  回归断言失败：")
            for f in reg_failures:
                print(f)
        else:
            print("  所有回归断言通过")
        print("  " + "-" * 56)

    # ── 验收标准（矩阵口径，DB生产真实值） ───────────────────────────────────
    print("\n  验收标准（DB生产口径，来自 matrix_distribution）：")
    print(f"  注：covered_full 旧推断值 ~85 已弃用，DB真实口径 ~48-50（含 pending 的3/3）")

    # C1: covered ≥2家 ≥ 70%  (复核后可比价潜力)
    c1 = llm_cmp2_any / max(anchors_total, 1) >= 0.70
    print(f"  [{'OK  ' if c1 else 'FAIL'}] 复核后可比价潜力（covered ≥2家）>= 70%: "
          f"{llm_cmp2_any}/{anchors_total} = {llm_cmp2_any/max(anchors_total,1)*100:.1f}%")

    # C2: 可比价锚点（quoted ≥2家）≥ 70%  [核心指标]
    c2 = quoted_ge_2_count / max(anchors_total, 1) >= 0.70
    print(f"  [{'OK  ' if c2 else 'FAIL'}] 可比价锚点（quoted ≥2家）>= 70%:      "
          f"{quoted_ge_2_count}/{anchors_total} = {quoted_ge_2_count/max(anchors_total,1)*100:.1f}%")

    # C3: N家完整自动比价（N/N quoted）≥ 25%  [DB口径，LLM波动±5]
    c3 = quoted_full_count / max(anchors_total, 1) >= 0.25
    print(f"  [{'OK  ' if c3 else 'FAIL'}] {N}家完整自动比价（{N}/{N} quoted）>= 25%:  "
          f"{quoted_full_count}/{anchors_total} = {quoted_full_count/max(anchors_total,1)*100:.1f}%")

    # C4: N家完整覆盖（含 pending）≥ 50%  [DB真实口径；旧90%是错误推断]
    c4 = covered_full_count / max(anchors_total, 1) >= 0.50
    print(f"  [{'OK  ' if c4 else 'FAIL'}] 人工复核后完整潜力（{N}/{N} covered）>= 50%: "
          f"{covered_full_count}/{anchors_total} = {covered_full_count/max(anchors_total,1)*100:.1f}%")

    # C5a/b: distribution totals must match anchors_total (proves no truncation)
    c5 = q_total == anchors_total and c_total == anchors_total
    print(f"  [{'OK  ' if c5 else 'FAIL'}] 分布合计 = {anchors_total}: "
          f"quoted_sum={q_total}  covered_sum={c_total}")

    # C5f: /llm-fill and /bid-matrix must return identical matrix_distribution — HARD FAIL
    # c5f already computed above; /bid-matrix is the production source of truth
    if c5f:
        print(f"  [OK  ] /llm-fill == /bid-matrix matrix_distribution（同源校验通过）")
    else:
        print(f"  [FAIL] /llm-fill != /bid-matrix matrix_distribution（生产主口径不一致，设计违反）")
        print(f"         llm-fill:  {md}")
        print(f"         bid-matrix: {bm_md}")

    # C6: all suppliers covered, no fill errors
    c6a = len(llm["per_supplier_fill"]) == len(sids)
    print(f"  [{'OK  ' if c6a else 'FAIL'}] 覆盖全部供应商: {len(llm['per_supplier_fill'])}/{len(sids)}")
    c6b = all(not f.get("error") for f in llm["per_supplier_fill"])
    print(f"  [{'OK  ' if c6b else 'FAIL'}] 所有供应商填表无 error（任一 error 禁止上线）")

    # C7: false_positive_align_count == 0
    c7 = fp_align_cnt == 0
    print(f"  [{'OK  ' if c7 else 'FAIL'}] false_positive_align_count = 0: 实际={fp_align_cnt}")

    # C8: readiness.can_finalize
    _readiness = llm.get("readiness") or {}
    _can_fin = _readiness.get("can_finalize", True)
    print(f"  [{'OK  ' if _can_fin else 'FAIL'}] readiness.can_finalize = {_can_fin}"
          + (f"  warnings={_readiness.get('warnings', [])}" if not _can_fin else ""))

    print()
    c9 = not (args.assert_regression and reg_failures)
    ok = c1 and c2 and c3 and c4 and c5 and c5f and c6a and c6b and c7 and c9 and _can_fin
    print("PASS -- LLM 填表链路成立" if ok else "PARTIAL -- 见上")
    print("=" * 60)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
