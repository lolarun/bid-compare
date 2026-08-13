"""e2e_diff.py — 严格逐行逐字段 diff（阶段二）。

输入：golden（审计后的标准答案）+ ExtractionDraft.rows
输出：summary / row_diff / field_metrics（写入 outputs/e2e_diff/<doc>/）

原则（用户阶段二/§8 要求）：
- 行匹配优先按整数 seq；当 PDF 无序号列（所有提取行 seq 为空）时，自动切换为
  内容对齐匹配（名称+规格+数量贪心最优一对一匹配），并在 summary 中标注 match_mode。
- 字段值比对只对 golden 来源为 raw 的字段（derived 字段不能用来评 OCR）。
- 金额字段同时给 exact 与 tolerance 两套命中率 + 误差。
- 不在此处下投产结论，只产出数字与差异清单，分类由人/后续步骤做。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 金额容差：分角级
_AMT_ABS = 0.05
_AMT_REL = 0.005

# golden字段 → ExtractionDraft 字段候选（含税合价/单价在 draft 里可能落在 total_price/unit_price）
_FIELD_MAP = {
    "name": ["name"],
    "spec": ["spec"],
    "model": ["model"],
    "unit": ["unit"],
    "brand": ["brand"],
    "qty": ["qty"],
    "tax_rate": ["tax_rate"],
    "unit_price_excl_tax": ["unit_price_excl_tax"],
    "total_price_excl_tax": ["total_price_excl_tax"],
    "tax_amount": ["tax_amount"],
    "unit_price_incl_tax": ["unit_price_incl_tax", "unit_price"],
    "total_price_incl_tax": ["total_price_incl_tax", "total_price"],
}
_STR_FIELDS = {"name", "spec", "model", "unit", "brand"}
_AMT_FIELDS = {"unit_price_excl_tax", "total_price_excl_tax", "tax_amount",
               "unit_price_incl_tax", "total_price_incl_tax"}
_NUM_FIELDS = {"qty", "tax_rate"} | _AMT_FIELDS


def _coerce_num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    if s in ("", "-"):
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    try:
        f = float(s)
        return f / 100.0 if pct else f
    except (ValueError, TypeError):
        return None


def _norm_str(s) -> str:
    return str(s or "").strip().replace(" ", "").replace("（", "(").replace("）", ")").lower()


def _extract_val(draft_fields: dict, golden_field: str):
    for cand in _FIELD_MAP.get(golden_field, [golden_field]):
        v = draft_fields.get(cand)
        if v not in (None, "", {}, []):
            return v
    return None


def _amt_match(a: float, b: float) -> tuple[bool, bool]:
    """returns (exact, tolerance)。exact: 完全相等(2位小数)；tolerance: 容差内。"""
    exact = round(a, 2) == round(b, 2)
    d = abs(a - b)
    tol = d <= _AMT_ABS or d / max(abs(b), 1.0) <= _AMT_REL
    return exact, tol


# ── 内容对齐（无序号文档专用）──────────────────────────────────────────────

def _name_sim(a: str | None, b: str | None) -> float:
    import difflib
    a = "".join((a or "").split()).lower()
    b = "".join((b or "").split()).lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _parse_num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("，", "").strip())
    except (ValueError, TypeError):
        return None


def _content_score(ex_fields: dict, g: dict) -> float:
    """行级匹配分（v2）：total_price + qty 作为强信号（50%），name + spec 辅助（50%）。

    设计原则：
    - 财务字段完全匹配时，即使名称有 OCR 差异也能正确配对；
    - 财务字段不可用时（null/0），退化为纯名称匹配，保持向后兼容。
    """
    # ── total_price 信号 ─────────────────────────────────────────────────
    et = _parse_num(ex_fields.get("total_price_incl_tax") or ex_fields.get("total_price"))
    gt = _parse_num(g.get("total_price_incl_tax") or g.get("total_price"))
    if et and gt and et > 0 and gt > 0:
        total_sig = 1.0 if abs(et - gt) / max(et, gt) < 0.005 else 0.0
    else:
        total_sig = 0.5  # 未知 → 中性，不加分不扣分

    # ── qty 信号 ─────────────────────────────────────────────────────────
    eq = _parse_num(ex_fields.get("qty"))
    gq = _parse_num(g.get("qty"))
    if eq and gq and eq > 0 and gq > 0:
        qty_sig = 1.0 if abs(eq - gq) / max(eq, gq) < 0.01 else 0.0
    else:
        qty_sig = 0.5

    # ── name 相似度 ──────────────────────────────────────────────────────
    # golden 的 name 可能是 Excel 编制者的另一套命名；raw_name 才是 PDF 字面名。
    # 抽取结果忠实于 PDF，故优先用 raw_name 比对（无 raw_name 时回退 name）。
    name_sim = _name_sim(ex_fields.get("name"), g.get("raw_name") or g.get("name"))

    # ── spec 相似度 ──────────────────────────────────────────────────────
    spec_a = "".join((ex_fields.get("spec") or "").split()).lower()
    spec_b = "".join((g.get("spec") or "").split()).lower()
    if spec_a and spec_b:
        spec_sim = 1.0 if spec_a == spec_b else (
            0.5 if spec_a in spec_b or spec_b in spec_a else 0.0
        )
    else:
        spec_sim = 0.5

    return total_sig * 0.35 + qty_sig * 0.15 + name_sim * 0.35 + spec_sim * 0.15


def _content_match(
    quote_lines: list,
    golden_rows: list,
    min_score: float = 0.40,
) -> tuple[dict[int, dict], dict[int, int]]:
    """保持行序的 DP 内容对齐（替代原贪心 sort-by-score 方案）。

    算法：标准 LCS 权重 DP（O(N·M)），保证：
    - extracted[i] 匹配 golden[j] 时，所有后续匹配的 golden 下标 > j。
    - 不允许串行：同一型号不同系统的两条 golden 行不会因为排名最高分
      而被同一条 extracted 行抢走第二次匹配机会。

    min_score 提高到 0.40：total+qty 中性时（各 0.5）基础分 = 0.35+0.075=0.425，
    name 为 0 的情况下还不够门槛，有效防止纯金额巧合的误配。

    Returns:
        gi_to_draft: golden_index → matched DraftRow
        gi_to_score: golden_index → match score × 1000 (int for display)
    """
    N, M = len(quote_lines), len(golden_rows)
    if N == 0 or M == 0:
        return {}, {}

    # 预计算分矩阵（过滤低分减少后续比较量）
    s_mat: list[list[float]] = []
    for i in range(N):
        row_scores = []
        for j in range(M):
            sc = _content_score(quote_lines[i].fields, golden_rows[j])
            row_scores.append(sc if sc >= min_score else 0.0)
        s_mat.append(row_scores)

    # DP: dp[i][j] = 前 i 条 extracted、前 j 条 golden 最优总分
    NEG = -1.0
    dp = [[NEG] * (M + 1) for _ in range(N + 1)]
    ch = [[0] * (M + 1) for _ in range(N + 1)]   # 0=skip_ex 1=skip_g 2=match
    for i in range(N + 1):
        dp[i][0] = 0.0
    for j in range(M + 1):
        dp[0][j] = 0.0

    for i in range(1, N + 1):
        for j in range(1, M + 1):
            # skip extracted[i-1]
            best = dp[i - 1][j]; best_ch = 0
            # skip golden[j-1]
            if dp[i][j - 1] > best:
                best = dp[i][j - 1]; best_ch = 1
            # match extracted[i-1] ↔ golden[j-1]
            sc = s_mat[i - 1][j - 1]
            if sc > 0 and dp[i - 1][j - 1] >= 0:
                cand = dp[i - 1][j - 1] + sc
                if cand > best:
                    best = cand; best_ch = 2
            dp[i][j] = best
            ch[i][j] = best_ch

    # 回溯
    gi_to_draft: dict[int, object] = {}
    gi_to_score: dict[int, int] = {}
    i, j = N, M
    while i > 0 and j > 0:
        c = ch[i][j]
        if c == 2:
            gi_to_draft[j - 1] = quote_lines[i - 1]
            gi_to_score[j - 1] = int(s_mat[i - 1][j - 1] * 1000)
            i -= 1; j -= 1
        elif c == 0:
            i -= 1
        else:
            j -= 1

    return gi_to_draft, gi_to_score


def diff_doc(doc_name: str, golden: dict, draft_rows: list, field_sources: dict | None = None) -> dict:
    """golden vs draft rows → 行级+字段级指标 + 逐行 diff。

    draft_rows: list of objects with .row_type, .fields (dict), .source_ref(.to_dict())。
    field_sources: golden 字段来源标签 dict（raw/derived/...）；None 则全部当 raw。

    当 PDF 无序号列（所有提取行 seq 均为空）时，自动切换内容对齐模式（content_align），
    summary 中含 match_mode="content_align" 和 content_match_scores 分布。
    """
    field_sources = field_sources or {}

    golden_rows = [r for r in golden["rows"] if str(r.get("seq", "")).strip().isdigit()]
    golden_by_seq = {str(r["seq"]): r for r in golden_rows}

    quote_lines = [r for r in draft_rows if r.row_type == "quote_line"]
    # 提取行按 seq 索引；统计重复
    seq_count: dict[str, int] = {}
    draft_by_seq: dict[str, object] = {}
    for r in quote_lines:
        seq = str(r.fields.get("seq") or "").strip()
        if seq.isdigit():
            seq_count[seq] = seq_count.get(seq, 0) + 1
            draft_by_seq.setdefault(seq, r)

    golden_seqs = set(golden_by_seq)
    draft_seqs = set(draft_by_seq)
    matched = sorted(golden_seqs & draft_seqs, key=int)
    missing = sorted(golden_seqs - draft_seqs, key=int)
    extra = sorted(draft_seqs - golden_seqs, key=int)
    duplicate = sorted((s for s, c in seq_count.items() if c > 1), key=int)
    no_seq_rows = sum(1 for r in quote_lines
                      if not str(r.fields.get("seq") or "").strip().isdigit())

    # ── 无序号文档：切换内容对齐 ─────────────────────────────────────────
    # 触发条件：所有提取行 seq 均无效（文档本身无序号列）
    use_content_align = (len(draft_by_seq) == 0 and len(quote_lines) > 0)
    content_match_info: dict = {}

    if use_content_align:
        # 内容对齐使用全部 golden 行（不过滤 seq）
        all_golden_rows = golden["rows"]
        gi_to_draft, gi_to_score = _content_match(quote_lines, all_golden_rows)
        # 构建对齐对列表，复用下方字段统计循环
        content_pairs: list[tuple[dict, object]] = [
            (all_golden_rows[gi], draft_r)
            for gi, draft_r in gi_to_draft.items()
        ]
        matched_count = len(content_pairs)
        missing_count = len(all_golden_rows) - matched_count
        scores = sorted(gi_to_score.values())
        content_match_info = {
            "match_mode": "content_align",
            "content_match_total": matched_count,
            "content_match_unmatched_golden": missing_count,
            "content_match_score_p25": scores[len(scores) // 4] if scores else None,
            "content_match_score_p50": scores[len(scores) // 2] if scores else None,
            "content_match_score_p75": scores[3 * len(scores) // 4] if scores else None,
        }
    else:
        content_pairs = []

    # ── 字段级统计 ────────────────────────────────────────────────────────
    field_stats: dict[str, dict] = {}
    row_diffs: list[dict] = []

    # Determine which pairs to iterate for field stats
    iter_pairs: list[tuple[dict, object]]  # (golden_row, draft_row)
    if use_content_align:
        iter_pairs = content_pairs
    else:
        iter_pairs = [(golden_by_seq[seq], draft_by_seq[seq]) for seq in matched]

    for g, d in iter_pairs:
        seq = str(g.get("seq", "")) if not use_content_align else str(g.get("seq", "?"))
        df = d.fields
        for gf in _FIELD_MAP:
            src = field_sources.get(gf, "raw")
            gv = g.get(gf)
            if gv in (None, "", "-"):
                continue  # golden 无该字段值，不计
            ev = _extract_val(df, gf)
            st = field_stats.setdefault(gf, {
                "source": src, "graded": 0, "exact": 0, "tolerance": 0,
                "present": 0, "abs_err_sum": 0.0, "rel_err_sum": 0.0,
            })
            # derived 字段只统计存在性，不计准确率（不能评 OCR）
            gradeable = (src == "raw")
            if ev not in (None, "", "-"):
                st["present"] += 1
            if not gradeable:
                continue
            st["graded"] += 1
            if gf in _STR_FIELDS:
                if _norm_str(gv) == _norm_str(ev):
                    st["exact"] += 1
                    st["tolerance"] += 1
                else:
                    row_diffs.append({"seq": seq, "field": gf,
                                      "golden": gv, "extracted": ev, "kind": "str"})
            else:  # numeric / amount
                gn, en = _coerce_num(gv), _coerce_num(ev)
                if gn is None:
                    st["graded"] -= 1
                    continue
                if en is None:
                    row_diffs.append({"seq": seq, "field": gf,
                                      "golden": gn, "extracted": None, "kind": "missing"})
                    continue
                if gf in _AMT_FIELDS:
                    ex, tol = _amt_match(en, gn)
                else:  # qty / tax_rate exact-ish
                    ex = round(en, 4) == round(gn, 4)
                    tol = abs(en - gn) <= max(abs(gn) * 0.01, 0.001)
                if ex:
                    st["exact"] += 1
                if tol:
                    st["tolerance"] += 1
                st["abs_err_sum"] += abs(en - gn)
                st["rel_err_sum"] += abs(en - gn) / max(abs(gn), 1.0)
                if not tol:
                    row_diffs.append({"seq": seq, "field": gf, "golden": gn,
                                      "extracted": en, "kind": "num",
                                      "abs_err": round(abs(en - gn), 4)})
    # 汇总字段指标
    matched_count_for_rate = len(content_pairs) if use_content_align else len(matched)
    field_metrics = {}
    for gf, st in field_stats.items():
        graded = st["graded"]
        field_metrics[gf] = {
            "source": st["source"],
            "graded": graded,
            "present_rate": round(st["present"] / matched_count_for_rate, 4) if matched_count_for_rate else 0,
            "exact_rate": round(st["exact"] / graded, 4) if graded else None,
            "tolerance_rate": round(st["tolerance"] / graded, 4) if graded else None,
            "mean_abs_err": round(st["abs_err_sum"] / graded, 4) if (graded and gf in _AMT_FIELDS) else None,
            "mean_rel_err": round(st["rel_err_sum"] / graded, 5) if (graded and gf in _AMT_FIELDS) else None,
        }

    # ── 金额：matched / no_seq / 全部 quote_line（含税合价）─────────────────
    def _sum_incl(rows_iter) -> float:
        s = 0.0
        for r in rows_iter:
            n = _coerce_num(_extract_val(r.fields, "total_price_incl_tax"))
            if n:
                s += n
        return round(s, 2)

    matched_rows = ([dr for _g, dr in content_pairs] if use_content_align
                    else [draft_by_seq[s] for s in matched])
    no_seq_draft = [r for r in quote_lines
                    if not str(r.fields.get("seq") or "").strip().isdigit()]
    matched_incl_total = _sum_incl(matched_rows)
    no_seq_incl_total = _sum_incl(no_seq_draft)
    all_incl_total = _sum_incl(quote_lines)

    declared = golden.get("declared_total")
    declared_diff = (round(all_incl_total - declared, 2)
                     if isinstance(declared, (int, float)) else None)

    # ── 行级召回/精确率（precision 分母 = 全部 quote_line，含 no_seq）────────
    all_golden_count = len(golden["rows"])
    matched_count = len(content_pairs) if use_content_align else len(matched)
    unmatched_extracted = len(quote_lines) - matched_count  # 抽出但未对上 golden 的行
    row_recall = round(matched_count / all_golden_count, 4) if all_golden_count else 0
    # 精确率：matched / 全部 quote_line（no_seq 与多余行都进分母，不再隐藏）
    row_precision = round(matched_count / len(quote_lines), 4) if quote_lines else 0

    summary = {
        "doc": doc_name,
        "row_level": {
            "golden_rows": all_golden_count,
            "extracted_quote_lines": len(quote_lines),
            "extracted_with_seq": len(draft_by_seq),
            "no_seq_rows": no_seq_rows,
            "matched": matched_count,
            "unmatched_extracted": unmatched_extracted,   # 抽出但未匹配（含 no_seq）
            "missing": missing if not use_content_align else [],
            "missing_count": (all_golden_count - matched_count),
            "extra": extra,
            "duplicate": duplicate,
            "row_recall": row_recall,
            "row_precision": row_precision,
            **content_match_info,
        },
        "document_level": {
            "declared_total": declared,
            "matched_incl_total": matched_incl_total,
            "no_seq_incl_total": no_seq_incl_total,
            "all_quote_lines_incl_total": all_incl_total,
            "declared_vs_all_diff": declared_diff,        # 全 quote_line 总额 − 声明总额
        },
        "field_metrics": field_metrics,
    }
    return {"summary": summary, "row_diffs": row_diffs,
            "row_records": [{"seq": s} for s in matched]}


def write_outputs(doc_name: str, result: dict, out_root: Path | None = None):
    out_root = out_root or (REPO / "outputs" / "e2e_diff")
    d = out_root / doc_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "field_metrics.json").write_text(
        json.dumps(result["summary"]["field_metrics"], ensure_ascii=False, indent=2), encoding="utf-8")
    with (d / "row_diff.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "field", "golden", "extracted", "kind", "abs_err"])
        for rd in result["row_diffs"]:
            if not isinstance(rd, dict):
                continue
            w.writerow([rd.get("seq"), rd.get("field"), rd.get("golden"),
                        rd.get("extracted"), rd.get("kind"), rd.get("abs_err", "")])
    return d
