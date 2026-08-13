"""Deterministic gate-misfire audit for Project 62 valves.

Classifies every pending/exclude cell into one of 6 audit classes to separate
"model didn't understand" from "post-LLM deterministic gate false-kill", and
quantifies the recoverable ceiling if canonical normalization / OCR were fixed.

Input : outputs/_gate_audit_raw.json  (produced by _dump_gate_cells.py)
Output: outputs/gate_audit_project62.csv  (full per-cell table, UTF-8)
        outputs/gate_audit_project62.md   (readable table + stats + verdict)
        ASCII summary to stdout
"""
from __future__ import annotations
import csv, json, os, sqlite3, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

RAW = ROOT / "outputs" / "_gate_audit_raw.json"
cells = json.loads(RAW.read_text(encoding="utf-8"))

# ── current align (quoted) count per anchor, to compute comparability flips ──
conn = sqlite3.connect("data/mempas.db")
align_suppliers: dict[str, set[int]] = defaultdict(set)
rows = conn.execute(
    "SELECT g.anchor_seq, i.supplier_id FROM bid_alignment_items i "
    "JOIN bid_alignment_groups g ON i.group_id=g.id "
    "WHERE g.project_id=62 AND g.status='confirmed' AND g.tender_list_session_id=1 "
    "AND i.action='align'"
).fetchall()
for seq, sid in rows:
    align_suppliers[str(seq)].add(sid)
# total anchors
total_anchors = conn.execute(
    "SELECT COUNT(*) FROM bid_alignment_groups "
    "WHERE project_id=62 AND status='confirmed' AND tender_list_session_id=1"
).fetchone()[0]
conn.close()

# ── valve family / subtype helpers ──────────────────────────────────────────
FAMILY = {
    "减压阀": "减压阀", "减压阀组": "减压阀",
    "止回阀": "止回阀", "橡胶瓣止回阀": "止回阀",
    "球阀": "球阀", "闸阀": "闸阀", "截止阀": "截止阀",
    "蝶阀": "蝶阀", "电动蝶阀": "蝶阀",
    "疏水阀": "疏水阀", "安全阀": "安全阀", "调节阀": "调节阀",
    "电动阀": "电动阀", "电磁阀": "电磁阀", "旋塞阀": "旋塞阀",
    "Y型过滤器": "过滤器", "过滤器": "过滤器",
    "流量测试": "__nonvalve__", "真空破坏器": "__nonvalve__",
}
_NAME_FAMILY_KW = ["减压阀", "止回阀", "球阀", "闸阀", "截止阀", "蝶阀",
                   "疏水阀", "安全阀", "调节阀", "过滤器", "倒流防止器", "隔断阀"]
_CHECK_SUBTYPES = ["橡胶瓣", "旋启式", "缓闭式", "升降式", "节能消声", "消声", "微阻缓闭"]
_OCR_GARBAGE = {"阀阀", "给排水", "低压力侧", "低压力测"}


def family(vt: str | None, name: str) -> str | None:
    if vt and vt in FAMILY:
        return FAMILY[vt]
    if vt:
        return vt
    for kw in _NAME_FAMILY_KW:
        if kw in (name or ""):
            if kw in ("减压阀",): return "减压阀"
            if kw in ("止回阀",): return "止回阀"
            return kw
    return None


def check_subtype(name: str) -> str:
    for st in _CHECK_SUBTYPES:
        if st in (name or ""):
            return "消声" if st in ("节能消声", "消声") else st
    if "橡胶瓣" in (name or ""):
        return "橡胶瓣"
    return "generic"


def is_garbage(name: str) -> bool:
    n = (name or "").strip()
    if n in _OCR_GARBAGE:
        return True
    # no recognizable valve keyword and looks degenerate
    if not any(kw in n for kw in _NAME_FAMILY_KW) and len(n) <= 4:
        return True
    return False


def has(flags, key):
    return any(f == key or f.startswith(key + ":") for f in flags)


def classify(c: dict) -> tuple[str, str]:
    """Return (audit_class, conflict_type)."""
    flags = c["flags"]
    a, q = c["anchor_canonical"], c["quote_canonical"]
    a_name, q_name = c["anchor_name"], c["quote_material"]
    a_vt, q_vt = a.get("valve_type"), q.get("valve_type")
    a_dn, q_dn = a.get("dn"), q.get("dn")
    a_pn, q_pn = a.get("pn"), q.get("pn")

    # 1. valve_type_conflict safety gate = genuine cross-type (non-valve / wrong valve)
    if has(flags, "valve_type_conflict"):
        vt = next((f.split(":", 1)[1] for f in flags if f.startswith("valve_type_conflict:")), "?")
        return "true_subtype_conflict", f"valve_type_conflict (anchor={a_vt} vs quote={vt}; 跨类/非阀)"

    # 2. structural: shared quote row / aggregation / anchor-claim conflict
    if has(flags, "dup_qids") or has(flags, "ac_conflict") or has(flags, "agg_conflict"):
        tag = next((f for f in flags if f.startswith(("dup_qids", "ac_conflict", "agg_conflict"))), "")
        return "duplicate_or_split_issue", f"{tag} (一报价行被多锚点引用/聚合占用)"

    # 3. explicit DN mismatch → genuinely different size
    if a_dn and q_dn and a_dn != q_dn:
        return "real_missing_or_no_safe_quote", f"DN冲突 {a_dn}≠{q_dn} (真不同口径)"

    # 4. explicit PN mismatch → genuinely different rating (note over-spec)
    if a_pn and q_pn and a_pn != q_pn:
        over = ""
        try:
            if int(q_pn[2:]) > int(a_pn[2:]):
                over = " [报价PN更高，工程上可能可接受→建议人工]"
        except Exception:
            pass
        cls = "unknown_need_manual" if over else "real_missing_or_no_safe_quote"
        return cls, f"PN冲突 {a_pn}≠{q_pn}{over}"

    # 5. OCR garbage quote name (canonical lost) and not already corrected
    if is_garbage(q_name) and "ocr_corrected" not in flags:
        return "ocr_correction_needed", f"报价品名OCR残缺『{q_name}』→canonical valve_type丢失"

    # 6. family comparison
    fam_a = family(a_vt, a_name)
    fam_q = family(q_vt, q_name)

    if fam_a and fam_q and fam_a == fam_q:
        if fam_a == "止回阀":
            sa, sq = check_subtype(a_name), check_subtype(q_name)
            if sa == sq:
                return "normalization_false_kill", f"止回阀同子型({sa})被规则误杀 (a_vt={a_vt}/q_vt={q_vt})"
            return "true_subtype_conflict", f"止回阀子型不同: 锚点『{sa}』 vs 报价『{sq}』"
        # same family, qualifier-only difference (减压阀组 vs 减压阀 etc.)
        return "normalization_false_kill", f"{fam_a}同族被规则误杀: a_vt『{a_vt}』 vs q_vt『{q_vt}』"

    # 7. cross-family or unresolved
    if "ocr_corrected" in flags:
        return "duplicate_or_split_issue" if (has(flags, "dup_qids")) else "ocr_correction_needed", \
               f"已OCR纠错但残留冲突 (a_vt={a_vt}/q_vt={q_vt})"
    if fam_a and fam_q and fam_a != fam_q:
        return "true_subtype_conflict", f"跨阀型: 锚点{fam_a} vs 报价{fam_q}"
    return "unknown_need_manual", f"证据不足 (a_vt={a_vt}/q_vt={q_vt}, fam_a={fam_a}/fam_q={fam_q})"


# ── classify all cells ──────────────────────────────────────────────────────
for c in cells:
    c["audit_class"], c["conflict_type"] = classify(c)

# ── aggregate stats ─────────────────────────────────────────────────────────
CLASSES = [
    "normalization_false_kill",
    "ocr_correction_needed",
    "true_subtype_conflict",
    "duplicate_or_split_issue",
    "real_missing_or_no_safe_quote",
    "unknown_need_manual",
]
cls_cells = defaultdict(int)
cls_anchors = defaultdict(set)
for c in cells:
    cls_cells[c["audit_class"]] += 1
    cls_anchors[c["audit_class"]].add(c["anchor_seq"])

# ── comparability impact ────────────────────────────────────────────────────
# baseline: anchors with >=2 align suppliers
def comparable_count(extra_supplier_by_anchor: dict[str, set[int]]) -> int:
    cnt = 0
    seqs = set(align_suppliers) | set(extra_supplier_by_anchor)
    for seq in seqs:
        sup = set(align_suppliers.get(seq, set())) | extra_supplier_by_anchor.get(seq, set())
        if len(sup) >= 2:
            cnt += 1
    return cnt

baseline = comparable_count({})

# if normalization_false_kill cells were promoted to quoted
norm_extra: dict[str, set[int]] = defaultdict(set)
for c in cells:
    if c["audit_class"] == "normalization_false_kill":
        norm_extra[c["anchor_seq"]].add(c["supplier_id"])
after_norm = comparable_count(norm_extra)

# if ALSO ocr_correction_needed promoted
ocr_extra = defaultdict(set, {k: set(v) for k, v in norm_extra.items()})
for c in cells:
    if c["audit_class"] == "ocr_correction_needed":
        ocr_extra[c["anchor_seq"]].add(c["supplier_id"])
after_norm_ocr = comparable_count(ocr_extra)

# ── write CSV ───────────────────────────────────────────────────────────────
csv_path = ROOT / "outputs" / "gate_audit_project62.csv"
with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["anchor_seq", "anchor_name", "anchor_spec", "supplier_name",
                "quote_id", "quote_material", "quote_spec", "flags", "cos",
                "anchor_canonical", "quote_canonical", "conflict_type",
                "audit_class", "llm_evidence"])
    for c in sorted(cells, key=lambda x: (int(x["anchor_seq"]) if x["anchor_seq"].isdigit() else 999,
                                          x["supplier_id"])):
        w.writerow([
            c["anchor_seq"], c["anchor_name"], c["anchor_spec"], c["supplier_name"],
            c["quote_id"], c["quote_material"], c["quote_spec"],
            ",".join(c["flags"]), c["cos"],
            json.dumps({k: v for k, v in c["anchor_canonical"].items() if v}, ensure_ascii=False),
            json.dumps({k: v for k, v in c["quote_canonical"].items() if v}, ensure_ascii=False),
            c["conflict_type"], c["audit_class"],
            (c["evidence"] or "")[:200],
        ])

# ── write Markdown ──────────────────────────────────────────────────────────
md = []
md.append("# Project 62 阀门 — Deterministic Gate 误杀审计\n")
md.append(f"- 锚点总数: **{total_anchors}**  |  当前可比(≥2家quoted): **{baseline}/{total_anchors} = {baseline/total_anchors*100:.1f}%**")
md.append(f"- pending/exclude cell 总数: **{len(cells)}**\n")

md.append("## 1. 分类聚合\n")
md.append("| audit_class | cell 数 | 涉及 anchor 数 | 性质 |")
md.append("|---|---|---|---|")
NATURE = {
    "normalization_false_kill": "规则误杀 — 修 canonical 归一化可救",
    "ocr_correction_needed": "OCR错字 — 修 OCR/纠错可救",
    "true_subtype_conflict": "真子型/跨型冲突 — 不得自动quoted",
    "duplicate_or_split_issue": "结构问题(去重/拆分) — 单列，非模型问题",
    "real_missing_or_no_safe_quote": "确实无安全报价 — 任何模型都救不了",
    "unknown_need_manual": "证据不足 — 人工确认",
}
for cl in CLASSES:
    md.append(f"| {cl} | {cls_cells[cl]} | {len(cls_anchors[cl])} | {NATURE[cl]} |")

md.append("\n## 2. 可比率提升测算\n")
md.append(f"- 当前 baseline: **{baseline}/{total_anchors} = {baseline/total_anchors*100:.1f}%**")
md.append(f"- 修 `normalization_false_kill` → quoted: **{after_norm}/{total_anchors} = {after_norm/total_anchors*100:.1f}%**  (+{after_norm-baseline} 锚点)")
md.append(f"- 再修 `ocr_correction_needed` → quoted: **{after_norm_ocr}/{total_anchors} = {after_norm_ocr/total_anchors*100:.1f}%**  (再 +{after_norm_ocr-after_norm} 锚点)")
md.append(f"- `true_subtype_conflict` 强制保持 pending（不计入提升）")
md.append(f"- `duplicate_or_split_issue` 单列为后续结构问题（不计入模型问题）\n")

md.append("## 3. 全量审计表\n")
md.append("| # | 锚点 | 供应商 | qid | 报价品名 | 报价规格 | flags | conflict_type | **audit_class** |")
md.append("|---|---|---|---|---|---|---|---|---|")
for c in sorted(cells, key=lambda x: (int(x["anchor_seq"]) if x["anchor_seq"].isdigit() else 999,
                                      x["supplier_id"])):
    sn = c["supplier_name"].replace("上海", "").replace("有限公司", "")[:8]
    md.append(f"| {c['anchor_seq']} | {c['anchor_name']} {c['anchor_spec']} | {sn} | "
              f"{c['quote_id']} | {c['quote_material']} | {c['quote_spec'][:24]} | "
              f"{','.join(c['flags'])[:30]} | {c['conflict_type'][:48]} | **{c['audit_class']}** |")

md_path = ROOT / "outputs" / "gate_audit_project62.md"
md_path.write_text("\n".join(md), encoding="utf-8")

# ── ASCII stdout summary ────────────────────────────────────────────────────
print("=" * 72)
print("  DETERMINISTIC GATE MISFIRE AUDIT — Project 62 valves")
print("=" * 72)
print(f"  anchors total       : {total_anchors}")
print(f"  baseline comparable : {baseline}/{total_anchors} = {baseline/total_anchors*100:.1f}%")
print(f"  pending/excl cells  : {len(cells)}")
print()
print(f"  {'audit_class':<32} {'cells':>6} {'anchors':>8}")
print("  " + "-" * 50)
for cl in CLASSES:
    print(f"  {cl:<32} {cls_cells[cl]:>6} {len(cls_anchors[cl]):>8}")
print()
print("  COMPARABILITY IMPACT")
print(f"    baseline                         : {baseline}/{total_anchors} = {baseline/total_anchors*100:.1f}%")
print(f"    + fix normalization_false_kill   : {after_norm}/{total_anchors} = {after_norm/total_anchors*100:.1f}%  (+{after_norm-baseline})")
print(f"    + fix ocr_correction_needed      : {after_norm_ocr}/{total_anchors} = {after_norm_ocr/total_anchors*100:.1f}%  (+{after_norm_ocr-after_norm})")
print()
# anchors recovered list
recovered = sorted([s for s in norm_extra
                    if len(align_suppliers.get(s, set()) | norm_extra[s]) >= 2
                    and len(align_suppliers.get(s, set())) < 2],
                   key=lambda x: int(x) if x.isdigit() else 999)
print(f"  anchors recovered by normalization fix: {recovered}")
print()
print(f"  CSV : {csv_path}")
print(f"  MD  : {md_path}")
print("=" * 72)
