"""bid_recommendation.py — 评标推荐逻辑（§10.3 拆分自 bid_matrix.py）。

包含：
  _compute_recommendation — 招标文件驱动的确定性推荐

设计约束（CLAUDE.md §5）：
  - 不自动定标、不拆单、不造权重
  - LLM 只解释此函数返回的确定性结果，不得改选
  - pending / excluded / basis_unconfirmed 行不参与评标总价
"""
from __future__ import annotations

from apps.api.core.enums import REC_BLOCKED, REC_CONDITIONAL


def _compute_recommendation(
    rows: list[dict],
    col_ids: list[int],
    supplier_labels: list[dict],
    total_anchors: int,
    checksum_by_col: dict[int, str],
    policy,
    use_submission_mode: bool = False,
) -> dict:
    """招标文件驱动的确定性推荐（不自动定标、不拆单、不造权重）。

    产出：评标总价排名(价格优选候选人) + 共同可比金额 + 未决行金额 + 非价格因素证据缺口 +
    三态 recommendation_level。LLM 仅据此解释，不得改选。

    B3 兼容期收尾（design/22 §B3）：col_id 在 submission 列模式下实际是
    BidSubmission.id。历史上字典键叫 "supplier_id"，名字与实际身份不符，且
    这个粒度从没人需要真正的供应商 FK（那个 FK 在 SupplierLabel.supplier_id
    上）。已改为通用列身份键 "id"（=col_id），"submission_id" 键并存
    （=col_id，仅 use_submission_mode 时非空，与 SupplierLabel 对称）。
    """
    label_by = {sl["id"]: sl for sl in supplier_labels}
    per = {cid: {"evaluated_total": 0.0, "confirmed_lines": 0, "qty_conflict_lines": 0,
                 "undecided_lines": 0, "undecided_amount": 0.0, "missing_lines": 0,
                 "anomaly_count": 0, "basis_unconfirmed_lines": 0,
                 "tax_assumed_lines": 0} for cid in col_ids}
    evaluable_by_line: list[set] = []
    for row in rows:
        eset: set = set()
        cell_by = {c["id"]: c for c in row["suppliers"]}
        for cid in col_ids:
            c = cell_by.get(cid, {})
            st = c.get("eval_status")
            if c.get("evaluable"):
                per[cid]["confirmed_lines"] += 1
                per[cid]["evaluated_total"] += c.get("eval_amount") or 0.0
                eset.add(cid)
                if st == "quantity_source_conflict":
                    per[cid]["qty_conflict_lines"] += 1
                if c.get("tax_basis_assumed"):
                    per[cid]["tax_assumed_lines"] += 1
            elif st in ("basis_unconfirmed", "alignment_pending"):
                per[cid]["undecided_lines"] += 1
                if st == "basis_unconfirmed":
                    per[cid]["basis_unconfirmed_lines"] += 1
                ref_unit = c.get("incl_unit") or c.get("price")
                if c.get("tender_qty") and ref_unit:
                    per[cid]["undecided_amount"] += round(float(c["tender_qty"]) * float(ref_unit), 2)
            else:
                per[cid]["missing_lines"] += 1
            if c.get("alert_level") == "red":
                per[cid]["anomaly_count"] += 1
        evaluable_by_line.append(eset)

    supplier_eval = []
    for cid in col_ids:
        p = per[cid]
        cs = checksum_by_col.get(cid, "unknown")
        full = (total_anchors > 0 and p["confirmed_lines"] == total_anchors)
        eligible = full and cs != "fail"
        supplier_eval.append({
            "id": cid,
            "submission_id": cid if use_submission_mode else None,
            "name": label_by.get(cid, {}).get("name"),
            "letter": label_by.get(cid, {}).get("letter"),
            "evaluated_total": round(p["evaluated_total"], 2),
            "confirmed_lines": p["confirmed_lines"],
            "total_anchors": total_anchors,
            "qty_conflict_lines": p["qty_conflict_lines"],
            "undecided_lines": p["undecided_lines"],
            "undecided_amount": round(p["undecided_amount"], 2),
            "missing_lines": p["missing_lines"],
            "anomaly_count": p["anomaly_count"],
            "tax_assumed_lines": p["tax_assumed_lines"],
            # 税口径"确认"须同时无未决口径与无假定口径；单一价格列属假定，非确认。
            "basis_confirmed": (p["basis_unconfirmed_lines"] == 0
                                and p["tax_assumed_lines"] == 0
                                and p["confirmed_lines"] > 0),
            "checksum_status": cs,
            "full_coverage": full,
            "eligible_for_ranking": eligible,
        })

    ranked = sorted(
        [s for s in supplier_eval if s["eligible_for_ranking"]],
        key=lambda s: s["evaluated_total"],
    )
    ranked_ids = [s["id"] for s in ranked]
    # 共同可比金额：所有入排名供应商**均可评标**的行
    common_lines = 0
    common_sub = {cid: 0.0 for cid in ranked_ids}
    for ridx, row in enumerate(rows):
        if ranked_ids and all(cid in evaluable_by_line[ridx] for cid in ranked_ids):
            common_lines += 1
            cell_by = {c["id"]: c for c in row["suppliers"]}
            for cid in ranked_ids:
                common_sub[cid] += cell_by[cid].get("eval_amount") or 0.0
    price_preferred = ranked[0] if ranked else None

    risks: list[str] = []
    for s in supplier_eval:
        nm = s["name"]
        if s["checksum_status"] == "fail":
            risks.append(f"{nm} 核验金额不符（checksum fail）")
        elif s["checksum_status"] in ("unknown", None):
            risks.append(f"{nm} 无声明总价核验（风险提示，未阻断）")
        if s["qty_conflict_lines"]:
            risks.append(
                f"{nm} {s['qty_conflict_lines']} 行报价数量≠招标数量，已按招标数量计入评标"
                "（标记 quantity_source_conflict，建议核实）"
            )
        if s.get("tax_assumed_lines"):
            risks.append(
                f"{nm} {s['tax_assumed_lines']} 行为单一价格列（无含税/不含税标注），"
                "已按招标含税单价要求纳入评标（税口径假定含税，建议核实）"
            )
        if s["undecided_lines"]:
            risks.append(
                f"{nm} {s['undecided_lines']} 行税口径/对齐未确认，未计入完整评标总价"
                f"（未决金额≈¥{s['undecided_amount']:,.0f}）"
            )
        if s["anomaly_count"]:
            risks.append(f"{nm} {s['anomaly_count']} 行同规格偏差异常，建议核实异常低价")
        if s["missing_lines"]:
            risks.append(f"{nm} {s['missing_lines']} 行缺报")

    reasons: list[str] = []
    if not ranked:
        level = REC_BLOCKED
        reasons.append("无任一供应商可形成完整含税评标总价（税口径/对齐未确认或缺报）→ 无价格优选候选人")
    else:
        level = REC_CONDITIONAL
        reasons.append("已得确定性『评标总价排名』与『价格优选候选人』")
        if policy.method == "unknown":
            reasons.append("评标法尚未确认 → 价格排名仅供参考，定标需人工确认招标文件评标法后方可进行")
        else:
            reasons.append("招标文件为合理低价评标价法且未给评分权重 → 综合评审需招标领导小组确认，非自动中标")

    # 非价格八项因素证据：系统无结构化证据 → 一律 missing（待评标小组）
    non_price = [{"factor": f, "evidence_status": "missing"} for f in policy.factors if f != "价格"]

    return {
        "recommendation_level": level,
        "recommendation_reasons": reasons,
        "risks": risks,
        "evaluation_policy": policy.to_dict(),
        "award_mode": policy.award_mode,
        "committee_required": policy.final_decision_requires_committee,
        "price_ranking": ranked,
        "price_preferred_candidate": price_preferred,
        "supplier_evaluation": supplier_eval,
        "common_comparable": {
            "ids": ranked_ids,
            "submission_ids": ranked_ids if use_submission_mode else None,
            "line_count": common_lines,
            "subtotals": {str(k): round(v, 2) for k, v in common_sub.items()},
        },
        "non_price_factors": non_price,
        "comprehensive_recommendation_status": "pending_committee",
    }
