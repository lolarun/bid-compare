"""Operation log API endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
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
):
    q = db.query(OperationLog)
    if user:
        q = q.filter(OperationLog.user == user)
    if module:
        q = q.filter(OperationLog.module == module)
    if action:
        q = q.filter(OperationLog.action.contains(action))
    if date_from:
        q = q.filter(OperationLog.created_at >= date_from)
    if date_to:
        q = q.filter(OperationLog.created_at <= date_to)

    total = q.count()
    items = q.order_by(OperationLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()

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
