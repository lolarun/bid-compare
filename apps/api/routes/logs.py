"""Operation log API endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.enums import LOG_MODULE_LABELS, ROLE_ADMIN
from apps.api.core.security import require_role
from apps.api.models.operation_log import OperationLog

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("", response_model=dict)
def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: str | None = None,
    module: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    stmt = select(OperationLog)
    if user:
        stmt = stmt.where(OperationLog.user == user)
    if module:
        stmt = stmt.where(OperationLog.module == module)
    if action:
        stmt = stmt.where(OperationLog.action.contains(action))
    if date_from:
        stmt = stmt.where(OperationLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OperationLog.created_at <= date_to)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(OperationLog.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": log.id,
                "time": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
                "user": log.user,
                "module": log.module,
                "action": log.action,
                "target": log.target,
                "result": log.result,
                "remark": log.remark,
            }
            for log in items
        ],
    }


@router.get("/modules", response_model=list)
def list_log_modules(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """Module filter vocabulary: the canonical registry plus whatever is stored.

    The UI must not carry its own hand-written option list — that is how the
    filter drifted out of sync with the values the code actually writes. Any
    module present in the table but missing from the registry is returned with
    its raw value as the label, so an unregistered writer is visible rather
    than silently unfilterable.
    """
    stored = set(db.scalars(select(OperationLog.module).distinct()).all())
    values = list(LOG_MODULE_LABELS) + sorted(stored - set(LOG_MODULE_LABELS))
    return [{"value": v, "label": LOG_MODULE_LABELS.get(v, v)} for v in values]


def write_log(
    db: Session,
    *,
    user: str,
    module: str,
    action: str,
    target: str = "",
    result: str = "成功",
    remark: str = "",
) -> OperationLog:
    """Helper to write an operation log entry from any service."""
    log = OperationLog(
        user=user,
        module=module,
        action=action,
        target=target,
        result=result,
        remark=remark,
    )
    db.add(log)
    db.commit()
    return log
