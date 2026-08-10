"""QuoteConfirmationService — batch quote confirmation authority.

Extracted from routes/quotes.py (batch_confirm) so the core write path
(ExtractionJob → BidSubmission + BidQuoteLine) lives in a testable service,
not inline in the route handler.

The route delegates here and is responsible only for HTTP mapping.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from apps.api.core.config import PROFESSION_MAP
from apps.api.models import (
    BrandTier,
    ExtractionJob,
    Material,
    Project,
    Supplier,
    BidSubmission,
    BidQuoteLine,
)
from apps.api.services.standardize import standardize_name
from apps.api.intelligence.price_basis import derive_price_basis
from apps.api.services.audit import normalize_row_type, write_domain_event, EVENT_BQL_CONFIRM
from apps.api.services.draft_integrity import (
    ARITHMETIC_FLAG,
    BLOCKED,
    COLUMN_SHIFT_FLAG,
    DUPLICATE_FLAG,
    REVIEW,
    TRUNCATION_FLAG,
    check_arithmetic,
    corroborate_truncation,
    detect_truncated_numbers,
    find_duplicate_rows,
)

log = logging.getLogger(__name__)

# Grand-total/subtotal name patterns — keep in sync with table_parser / routes/quotes.py.
_GRAND_TOTAL_NAME_RE = re.compile(
    r"价税合计|总计|合计金额|投标总价|^合计$|含税总计|含税合计|详见投标清单"
)


def _num_or_none(v: Any) -> float | None:
    return float(v) if v is not None else None


def _integrity_row(items: list[dict], i: int, flags: list[str]) -> dict:
    it = items[i]
    return {
        "index": i,
        "material": str(it.get("material") or ""),
        "spec": str(it.get("spec") or ""),
        "qty": it.get("qty"),
        "unit_price": it.get("unit_price"),
        "total_price": it.get("total_price"),
        "flags": flags,
        "reason": _INTEGRITY_REASONS.get(flags[0], "结构完整性存疑") if flags else "",
    }


_INTEGRITY_REASONS = {
    COLUMN_SHIFT_FLAG: "数据列数与表头不一致，按列名取到的值整体错位",
    DUPLICATE_FLAG: "与前面某行的名称/规格/数量/单价完全相同，疑似重复抽取",
    ARITHMETIC_FLAG: "数量×单价与合价对不上，三者中至少一个读错",
    TRUNCATION_FLAG: "该数值卡在本列的宽度上限且小数位偏少，疑似被截断",
}


def _truncation_from_items(items: list[dict]):
    """在 items 这一层做截断检测。

    截断只能从**原始文本**看出来——值一旦被 float() 解析过，`1956390.` 与
    `1956390.45` 就再也分不开了。所以这里只检查仍是字符串的原值；上游若已把价格
    转成数字，就必须在还是表格的时候调 detect_truncated_numbers（见 draft_integrity）。
    """
    keys = ("total_price", "total_price_incl_tax", "total_price_excl_tax",
            "unit_price", "unit_price_incl_tax", "unit_price_excl_tax", "qty")
    raw_keys = [k for k in keys
                if any(isinstance(it.get(k), str) and it.get(k) for it in items)]
    if not raw_keys:
        return None
    rows = [[str(it.get(k) if isinstance(it.get(k), str) else "") for k in raw_keys]
            for it in items]
    rep = detect_truncated_numbers(raw_keys, rows)
    return corroborate_truncation(rep, items) if rep.suspects else rep


def _gate_integrity(db: Session, items: list[dict]) -> dict:
    """列错位 / 重复行门。两者的处置**不同**，因为它们的合法性不同：

    - **列错位**：数据列数与表头不一致，按列名取到的每个值都可能是别的列的值。
      没有任何一种正常表格会这样，故一律阻断（422 + rollback）。上游表格来源必须
      在 `validation_flags` 里带上 `column_shift`（见 draft_integrity）；items 到这
      一层已经是 dict，自己发现不了。
    - **重复行**：**合法的重复真实存在**——同一型号阀门同量同价出现在给水和排水两
      个系统里，是正常清单。实测三份真实阀门文档各有 3~6 组这样的行，且逐行核对
      与 golden 完全一致。故 REVIEW 级重复只标注、不阻断；只有当重复金额占比越过
      阈值（domain_config）才升级为 BLOCKED——那种规模不可能是真实重复。

    放行 BLOCKED 行的唯一方式是用户在预览里标 `integrity_ack=true`，与派生金额门一致。
    返回给响应体的告警摘要；不改写任何原值。
    """
    dup = find_duplicate_rows(items)
    arith = check_arithmetic(items)
    trunc = _truncation_from_items(items)
    shifted = {i for i, it in enumerate(items)
               if COLUMN_SHIFT_FLAG in (it.get("validation_flags") or [])}
    dup_rows = dup.duplicate_row_indices
    arith_rows = set(arith.mismatch_indices)
    trunc_rows = trunc.suspect_row_indices if trunc else set()

    # 只标注、放行的那部分：写进 validation_flags，下游据此知道这行被怀疑过
    warn: dict[int, list[str]] = {}
    if dup.verdict == REVIEW:
        for i in sorted(dup_rows):
            warn.setdefault(i, []).append(DUPLICATE_FLAG)
    if arith.verdict == REVIEW:
        for i in sorted(arith_rows):
            warn.setdefault(i, []).append(ARITHMETIC_FLAG)
    # 截断永远只标注不阻断：值仍然近似正确（丢的是小数位），行本身可以入库，
    # 但逐行金额不能当精确值用，必须让人看见。
    for i in sorted(trunc_rows):
        warn.setdefault(i, []).append(TRUNCATION_FLAG)

    warn_rows: list[dict] = []
    for i, flags in sorted(warn.items()):
        existing = list(items[i].get("validation_flags") or [])
        items[i]["validation_flags"] = existing + [f for f in flags if f not in existing]
        warn_rows.append(_integrity_row(items, i, flags))

    blocking_dup = dup_rows if dup.verdict == BLOCKED else set()
    blocking_arith = arith_rows if arith.verdict == BLOCKED else set()
    block_rows = [
        _integrity_row(items, i,
                       ([COLUMN_SHIFT_FLAG] if i in shifted else [])
                       + ([DUPLICATE_FLAG] if i in blocking_dup else [])
                       + ([ARITHMETIC_FLAG] if i in blocking_arith else []))
        for i in sorted(shifted | blocking_dup | blocking_arith)
        if not items[i].get("integrity_ack")
    ]

    if block_rows:
        db.rollback()
        raise HTTPException(
            422,
            detail={
                "error": "structural_integrity_requires_review",
                "message": (
                    f"{len(block_rows)} 行未通过结构完整性检查"
                    f"（列错位 {len(shifted)} 行 / 重复 {len(blocking_dup)} 行 / "
                    f"算术不闭合 {len(blocking_arith)} 行，"
                    f"重复金额占比 {dup.amount_ratio:.1%}，"
                    f"算术错误率 {arith.error_rate:.1%}）。"
                    f"系统不会代为删除或重排，请核对原文后逐行确认。"
                ),
                "review_rows": block_rows[:50],
                "review_row_count": len(block_rows),
                "duplicates": dup.to_dict(),
                "arithmetic": arith.to_dict(),
            },
        )

    return {"duplicate_verdict": dup.verdict,
            "duplicate_rows": len(dup_rows),
            "duplicate_amount_ratio": round(dup.amount_ratio, 4),
            "column_shift_rows": len(shifted),
            "arithmetic": arith.to_dict(),
            "truncation": trunc.to_dict() if trunc else None,
            "warnings": warn_rows[:50]}


def confirm_batch(db: Session, body) -> dict:
    """将 OCR 提取结果暂存为 BidSubmission + BidQuoteLine（P0 新版）。

    `body` must have the same fields as BatchConfirmRequest in routes/quotes.py.

    Returns the response dict the route should return directly.
    """
    job = db.get(ExtractionJob, body.job_id)
    if not job:
        raise HTTPException(404, f"Job {body.job_id} not found")
    if job.type != "quote":
        raise HTTPException(400, f"Job type is {job.type}; must be 'quote'")
    if job.status != "done":
        raise HTTPException(400, f"Job status is {job.status}; must be 'done'")

    # ── Supplier 验证（弱关联：supplier_id 可选，有则校验状态）──────────────────
    supplier: Supplier | None = None
    if body.supplier_id is not None:
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier:
            raise HTTPException(404, f"Supplier {body.supplier_id} not found")
        if supplier.merge_status != "active":
            raise HTTPException(
                400,
                f"Supplier {supplier.name!r} merge_status={supplier.merge_status}，"
                "只允许选择 active 供应商",
            )
    else:
        if not body.supplier_name.strip():
            raise HTTPException(422, "陌生供应商必须提供 supplier_name")

    # ── Project（允许按名查找或创建，project 不是污染来源）─────────────────────
    project: Project | None = None
    if body.project_id:
        project = db.get(Project, body.project_id)
        if not project:
            raise HTTPException(404, f"Project {body.project_id} not found")
    elif body.project_name.strip():
        pname = body.project_name.strip()
        project = db.scalar(select(Project).where(Project.name == pname))
        if not project:
            project = Project(name=pname)
            db.add(project)
            db.flush()
    elif (job.context or {}).get("project_id"):
        ctx_pid = job.context["project_id"]
        project = db.get(Project, ctx_pid)
        if not project:
            raise HTTPException(
                400,
                f"Project {ctx_pid} from job context no longer exists; "
                "specify project_name or project_id to proceed.",
            )

    # ── 默认 category ──────────────────────────────────────────────────────────
    default_category = (
        body.category.strip()
        or (job.context or {}).get("category", "")
        or ""
    )
    if default_category and default_category not in PROFESSION_MAP:
        raise HTTPException(400, f"Unknown category: {default_category}")

    # ── Item list ──────────────────────────────────────────────────────────────
    raw_items: Any = (
        body.overrides
        if body.overrides is not None
        else (job.result or {}).get("items")
    )
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise HTTPException(422, f"Expected items list, got {type(raw_items).__name__}")

    # 早期校验：有 items 但 category 为空 → 立即拒绝，不创建空壳 submission
    _has_real_items = any(
        str(r.get("material") or "").strip() for r in raw_items if isinstance(r, dict)
    )
    if _has_real_items and not default_category:
        raise HTTPException(
            422,
            "category 不能为空：无法确定报价品类，入库中止。"
            "请在前端选择品类（如「阀门」）后重新点击「校对入库」。",
        )

    items: list[dict[str, Any]] = []
    shape_errors: list[dict] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            items.append(item)
        else:
            shape_errors.append({"row": idx + 1, "reason": f"not an object: {type(item).__name__}"})

    # ── 幂等：BidSubmission.batch_id 检查（一个 job 最多一条 BidSubmission）────────
    batch_id = f"BID-{job.id}"
    prior_submission = db.scalar(select(BidSubmission).where(BidSubmission.batch_id == batch_id))
    display_name = (
        body.supplier_name.strip()
        or (supplier.name if supplier else "")
        or (job.result or {}).get("supplier_name", "")
    )
    if prior_submission:
        # 同一文件→同一 job→同一 batch_id。废弃状态不能作为幂等命中。
        _stale = prior_submission.status in ("superseded", "rejected")
        prior_line_count = db.scalar(
            select(func.count(BidQuoteLine.id)).where(
                BidQuoteLine.submission_id == prior_submission.id
            )
        ) or 0
        if prior_line_count > 0 and not _stale:
            log.info(
                "batch_confirm: idempotent hit, submission_id=%d batch=%s lines=%d",
                prior_submission.id, batch_id, prior_line_count,
            )
            if job.lifecycle != "confirmed":
                job.lifecycle = "confirmed"
                db.commit()
            return {
                "status": "ok",
                "submission_id": prior_submission.id,
                "line_count": prior_line_count,
                "skipped_count": 0,
                "errors": [],
                "unknown_brands": [],
                "supplier_id": prior_submission.supplier_id,
                "project_id": project.id if project else None,
                "batch_id": batch_id,
                "idempotent": True,
            }
        if _stale:
            deleted = db.execute(
                delete(BidQuoteLine).where(BidQuoteLine.submission_id == prior_submission.id)
            ).rowcount
            log.warning(
                "batch_confirm: reviving %s submission_id=%d batch=%s "
                "(cleared %d stale lines → pending)",
                prior_submission.status, prior_submission.id, batch_id, deleted,
            )
            prior_submission.status = "pending"
            prior_submission.supplier_id = supplier.id if supplier else None
            if project:
                prior_submission.project_id = project.id
        else:
            log.warning(
                "batch_confirm: rebuilding empty shell submission_id=%d batch=%s",
                prior_submission.id, batch_id,
            )
        if display_name:
            prior_submission.supplier_raw_name = display_name
        if body.bid_status:
            prior_submission.bid_status = body.bid_status
        submission = prior_submission
    else:
        submission = BidSubmission(
            job_id=job.id,
            supplier_id=supplier.id if supplier else None,
            supplier_raw_name=display_name,
            project_id=project.id if project else None,
            batch_id=batch_id,
            status="pending",
            bid_status=body.bid_status,
        )
        db.add(submission)
        db.flush()

    job.lifecycle = "confirmed"

    if not items:
        db.commit()
        return {
            "status": "ok",
            "submission_id": submission.id,
            "line_count": 0,
            "skipped_count": 0,
            "errors": shape_errors,
            "unknown_brands": [],
            "supplier_id": submission.supplier_id,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
        }

    # ── 结构完整性门（doc/19 §L4）──────────────────────────────────────────────
    # 在写任何一行之前先看**表的形状**：列错位与重复行是下游唯一察觉不到的两类缺陷，
    # 错位后的金额仍是合法数字、重复行仍能通过逐行算术校验。两者都只标注和阻断，
    # 不删行、不改值、不猜正确列序——恢复正确值必须回原始页面重读。
    integrity = _gate_integrity(db, items)

    # ── 逐行处理 → BidQuoteLine ────────────────────────────────────────────────
    from apps.api.services.comparison import get_category_thresholds, determine_alert

    thresholds_cache: dict[str, dict] = {}
    line_count = 0
    skipped_count = 0
    errors: list[dict] = list(shape_errors)
    unknown_brands: set[str] = set()
    line_total_sum: float = 0.0
    missing_total_rows: list[dict] = []   # 原文无合价、未经人工确认的行

    for idx, item in enumerate(items):
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped_count += 1
                continue
            if _GRAND_TOTAL_NAME_RE.search(raw_name):
                log.info("batch_confirm: skipping aggregate row %r", raw_name)
                skipped_count += 1
                continue

            item_category = str(item.get("category") or "").strip() or default_category
            if not item_category or item_category not in PROFESSION_MAP:
                errors.append({"row": idx + 1, "reason": f"invalid category: {item_category!r}"})
                skipped_count += 1
                continue

            ai_std_name = str(item.get("standard_name") or "").strip()
            standard_name = ai_std_name if ai_std_name else standardize_name(raw_name, item_category)["standardized"]

            spec = str(item.get("standard_spec") or item.get("spec") or "").strip()

            # Material 查找（P0：仅查找，不创建）
            mat: Material | None = None
            matched_mid = item.get("matched_material_id")
            if matched_mid is not None:
                try:
                    mat = db.get(Material, int(matched_mid))
                except (ValueError, TypeError):
                    pass
            if not mat:
                mat = db.scalar(select(Material).where(
                    Material.category == item_category,
                    Material.standard_name == standard_name,
                    Material.spec == spec,
                ))

            brand = str(item.get("brand") or "").strip()
            brand_tier = ""
            if brand:
                bt = db.scalar(select(BrandTier).where(BrandTier.brand_name == brand))
                if bt:
                    brand_tier = bt.tier
                else:
                    unknown_brands.add(brand)

            qty = float(q) if (q := item.get("qty")) is not None else None

            # 价格口径桥接（§4/§9）：现场 re-derive，不信任客户端回传的 price_basis
            basis_info = derive_price_basis(item)
            price_basis = basis_info["price_basis"]
            confirmed_unit = float(cu) if (cu := item.get("unit_price")) is not None else None
            confirmed_total = float(ct) if (ct := item.get("total_price")) is not None else None
            price = confirmed_unit if confirmed_unit is not None else basis_info["effective_unit_price"]
            total = confirmed_total if confirmed_total is not None else basis_info["effective_total_price"]
            # 权威合价只能来自原文或人工补写，**系统不得自行派生**（doc/19 §L2）。
            #   ocr     — 原文读到（raw_total_price 非空）
            #   manual  — 原文没有、用户在预览中明确补写（override 带值）
            #   missing — 原文没有、也没人工补写 → 权威值保持 None，仅留候选
            # 2026-08-09 教训：静默派生既凭空造钱（亨通单行虚增约 2000 万），
            # 又让算术校验 |qty×price − total| 恒成立，把列错位行洗白。
            raw_total = _num_or_none(item.get("total_price"))
            raw_total_any = next(
                (v for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax")
                 if (v := _num_or_none(item.get(k))) is not None), None)
            derived_candidate = _num_or_none(item.get("derived_total_candidate"))
            if derived_candidate is None and raw_total_any is None and price and qty:
                derived_candidate = round(price * qty, 4)

            if raw_total_any is not None:
                # 上游若已派生过，这里绝不能再当成 ocr —— 用 item 自带的标记判定
                total_source = "manual" if item.get("total_is_manual") else "ocr"
                total = float(confirmed_total if confirmed_total is not None
                              else basis_info["effective_total_price"] or raw_total_any)
            else:
                total_source = "missing"
                total = None
                missing_total_rows.append({
                    "index": idx,
                    "material": raw_name,
                    "spec": str(item.get("spec") or ""),
                    "qty": qty,
                    "unit_price": price,
                    "derived_total_candidate": derived_candidate,
                    "reason": "原文无合价；需人工确认后方可入库",
                })

            deviation: float | None = None
            alert: str = ""
            if mat and price:
                ref = mat.ref_price_reasonable_low or mat.ref_price_median
                if ref and ref > 0:
                    if item_category not in thresholds_cache:
                        thresholds_cache[item_category] = get_category_thresholds(db, item_category)
                    deviation = round((price - ref) / ref, 4)
                    alert = determine_alert(deviation, thresholds_cache[item_category])

            extraction_meta = {
                "extraction_job_id": body.job_id,
                "source_ref": item.get("source_ref"),
                "raw_material": raw_name,
                "raw_spec": str(item.get("spec") or "").strip(),
                "raw_unit": str(item.get("unit") or "").strip(),
                "raw_remark": str(item.get("remark") or "").strip(),
                "material_type": str(item.get("material_type") or "").strip(),
                "canonical": item.get("canonical") or {},
                "validation_warning": item.get("validation_warning") or "",
                "normalized_material": str(item.get("normalized_material") or "").strip(),
                "ocr_correction_reason": str(item.get("ocr_correction_reason") or "").strip(),
                "price_basis": price_basis,
                "effective_unit_price": basis_info["effective_unit_price"],
                "effective_total_price": basis_info["effective_total_price"],
                "effective_unit_recovered": basis_info.get("effective_unit_recovered", False),
                "raw_unit_price": _num_or_none(item.get("unit_price")),
                "raw_unit_price_incl_tax": _num_or_none(item.get("unit_price_incl_tax")),
                "raw_unit_price_excl_tax": _num_or_none(item.get("unit_price_excl_tax")),
                "raw_total_price": _num_or_none(item.get("total_price")),
                "raw_total_price_incl_tax": _num_or_none(item.get("total_price_incl_tax")),
                "raw_total_price_excl_tax": _num_or_none(item.get("total_price_excl_tax")),
                "tax_rate": _num_or_none(item.get("tax_rate")),
                "tax_amount": _num_or_none(item.get("tax_amount")),
                "document_row_index": (
                    int(v) if (v := item.get("document_row_index")) is not None else None
                ),
                # 下游校验据此排除不携带算术信息的行，而不是把它们当成通过
                "total_source": total_source,
                "derived_total_candidate": derived_candidate,
                "validation_flags": (
                    list(item.get("validation_flags") or [])
                    + (["derived_total"] if total_source == "derived" else [])
                ),
                "raw_qty": _num_or_none(item.get("raw_qty")) if item.get("raw_qty") is not None else qty,
                "suggested_qty": _num_or_none(item.get("suggested_qty")),
            }

            line = BidQuoteLine(
                submission_id=submission.id,
                material_id=mat.id if mat else None,
                raw_name=raw_name,
                standard_name=standard_name,
                category=item_category,
                spec=spec,
                unit=str(item.get("unit") or ""),
                qty=qty,
                unit_price=price,
                unit_price_excl_tax=(
                    float(v) if (v := item.get("unit_price_excl_tax")) is not None else None
                ),
                tax_rate=(float(v) if (v := item.get("tax_rate")) is not None else None),
                total_price=total,
                brand=brand,
                brand_tier=brand_tier,
                remark=str(item.get("remark") or "")[:500],
                quote_date=str(item.get("quote_date") or ""),
                canonical=item.get("canonical"),
                extraction_meta=extraction_meta,
                deviation_pct=deviation,
                alert_level=alert,
                row_type=normalize_row_type(item.get("row_type")),
            )
            db.add(line)
            line_count += 1
            if total is not None:
                line_total_sum += total

        except Exception as e:
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped_count += 1

    # ── 派生金额安全闭环（doc/19 §L2）──────────────────────────────────────
    # 原文无合价且未经人工确认的行，一律阻断自动确认：回滚整个事务，不写
    # BidQuoteLine，不把 job 标成 confirmed。试点期采用最保守规则——**单行即阻断**，
    # 不用占比阈值：亨通实测单行列错位即可造成约 2000 万误差，5% 的行数门槛护不住。
    if missing_total_rows:
        db.rollback()
        raise HTTPException(
            422,
            detail={
                "error": "missing_total_requires_review",
                "message": (
                    f"{len(missing_total_rows)} 行原文无合价。系统不会代为计算，"
                    f"请在预览中人工补写或确认后再提交。"
                ),
                "review_rows": missing_total_rows[:50],
                "review_row_count": len(missing_total_rows),
            },
        )

    # 强校验：items 非空但全部被跳过 → 回滚并返回 422
    if items and line_count == 0:
        db.rollback()
        reason_summary = "; ".join({e["reason"] for e in errors[:3]}) if errors else "品类无效或所有行被过滤"
        raise HTTPException(
            422,
            f"所有 {len(items)} 行报价均被跳过，入库已回滚。原因：{reason_summary}",
        )

    write_domain_event(
        db, user="system", event_type=EVENT_BQL_CONFIRM,
        identity={
            "project_id": project.id if project else None,
            "submission_id": submission.id,
        },
        after={
            "line_count": line_count,
            "supplier_name": display_name,
            "category": default_category,
            "batch_id": batch_id,
        },
        meta={"skipped_count": skipped_count},
    )
    db.commit()

    # checksum 回写到 job.result
    try:
        doc_meta = (job.result or {}).get("_doc_meta") or {}
        declared = doc_meta.get("bid_total")
        if declared and float(declared) > 0 and line_count > 0:
            delta_pct = abs(line_total_sum - float(declared)) / float(declared) * 100
            cs_status = "pass" if delta_pct <= 5 else "fail"
            # 明细合计里掺了派生值时，"对得上"没有意义——可能只是刚好被凑出来的。
            # 走到这里说明没有未确认的派生行（否则上面已 422），checksum 可如实判定
        else:
            delta_pct = None
            cs_status = "unknown"
        job.result = {
            **(job.result or {}),
            "_checksum": {
                "declared": declared,
                "line_sum": round(line_total_sum, 2),
                "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
                "status": cs_status,
            },
        }
        ctx = dict(job.context or {})
        if submission.supplier_id and ctx.get("supplier_id") != submission.supplier_id:
            ctx["supplier_id"] = submission.supplier_id
            job.context = ctx
        db.add(job)
        db.commit()
    except Exception:
        log.exception("batch_confirm: checksum write failed for job %s", body.job_id)

    return {
        "status": "ok",
        "submission_id": submission.id,
        "line_count": line_count,
        "skipped_count": skipped_count,
        "missing_total_rows": 0,
        # 通过但被怀疑过的行：重复行已入库，但带着 duplicate_row 标记，
        # 前端应提示人工复核（REVIEW 不等于拒收——合法的重复真实存在）
        "integrity": integrity,
        "errors": errors,
        "unknown_brands": sorted(unknown_brands),
        "supplier_id": submission.supplier_id,
        "project_id": project.id if project else None,
        "batch_id": batch_id,
    }
