"""rebuild_submission_lines — 原子 BQL 重建服务（不提交事务）。

由 repair 脚本调用：多个 submission 在同一事务中重建，全部成功后由调用方统一 commit。
任何失败由调用方 rollback。
"""
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.config import PROFESSION_MAP
from apps.api.services.standardize import standardize_name

log = logging.getLogger(__name__)

_GRAND_TOTAL_NAME_RE = re.compile(
    r"价税合计|总计|合计金额|投标总价|^合计$|含税总计|含税合计|详见投标清单"
)


def rebuild_submission_lines(
    db: Session,
    submission_id: int,
    display_name: str,
    category: str,
    supplier_id: int | None = None,
    items_override: list | None = None,
) -> dict:
    """在不提交的情况下重建 BidSubmission 的 BidQuoteLine 行。

    调用方在所有 submission 重建完毕后负责 commit 或 rollback。

    Returns:
        dict with keys: line_count, skipped_count, errors
    Raises:
        ValueError: submission not found or category invalid
        RuntimeError: all items skipped (would produce empty BQL)
    """
    from apps.api.models.bid_submission import BidSubmission, BidQuoteLine
    from apps.api.models import ExtractionJob, Material, BrandTier
    from apps.api.services.comparison import get_category_thresholds, determine_alert

    if not category or category not in PROFESSION_MAP:
        raise ValueError(f"无效 category: {category!r}")

    submission = db.get(BidSubmission, submission_id)
    if submission is None:
        raise ValueError(f"BidSubmission {submission_id} 不存在")

    # Update metadata — unconditionally set supplier_id so passing None clears a wrong association
    submission.supplier_raw_name = display_name
    submission.supplier_id = supplier_id   # None is valid: clears stale association
    db.add(submission)
    db.flush()

    # Load items
    if items_override is not None:
        raw_items = items_override
    else:
        job = db.get(ExtractionJob, submission.job_id)
        if not job:
            raise ValueError(f"ExtractionJob {submission.job_id!r} 不存在")
        raw_items = (job.result or {}).get("items") or []

    # Delete existing BQL rows (rebuilding in-place)
    existing = db.scalars(
        select(BidQuoteLine).where(BidQuoteLine.submission_id == submission_id)
    ).all()
    for row in existing:
        db.delete(row)
    db.flush()

    thresholds_cache: dict[str, dict] = {}
    line_count = 0
    skipped_count = 0
    errors: list[dict] = []
    line_total_sum: float = 0.0

    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            skipped_count += 1
            continue
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped_count += 1
                continue
            if _GRAND_TOTAL_NAME_RE.search(raw_name):
                skipped_count += 1
                continue

            item_category = str(item.get("category") or "").strip() or category
            if not item_category or item_category not in PROFESSION_MAP:
                errors.append({"row": idx + 1, "reason": f"invalid category: {item_category!r}"})
                skipped_count += 1
                continue

            ai_std_name = str(item.get("standard_name") or "").strip()
            standard_name = ai_std_name if ai_std_name else standardize_name(raw_name, item_category)["standardized"]
            spec = str(item.get("standard_spec") or item.get("spec") or "").strip()

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

            price = float(p) if (p := item.get("unit_price")) is not None else None
            qty = float(q) if (q := item.get("qty")) is not None else None
            total = item.get("total_price")
            # 与 confirm 同一规则：不派生权威合价，只留候选（doc/19 §L2）。
            derived_candidate = None
            if total is None and price is not None and qty is not None:
                derived_candidate = round(price * qty, 4)
            if total is not None:
                total = float(total)

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
                "extraction_job_id": submission.job_id,
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
            }

            line = BidQuoteLine(
                submission_id=submission_id,
                material_id=mat.id if mat else None,
                raw_name=raw_name,
                standard_name=standard_name,
                category=item_category,
                spec=spec,
                unit=str(item.get("unit") or ""),
                qty=qty,
                unit_price=price,
                unit_price_excl_tax=(float(v) if (v := item.get("unit_price_excl_tax")) is not None else None),
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
            )
            db.add(line)
            line_count += 1
            if total is not None:
                line_total_sum += total

        except Exception as e:
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped_count += 1

    if raw_items and line_count == 0:
        reason_summary = "; ".join({e["reason"] for e in errors[:3]}) if errors else "所有行被过滤"
        raise RuntimeError(
            f"submission {submission_id}: 所有 {len(raw_items)} 行均被跳过。原因：{reason_summary}"
        )

    db.flush()
    return {
        "line_count": line_count,
        "skipped_count": skipped_count,
        "errors": errors,
        "line_total_sum": line_total_sum,
    }
