"""User management CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.enums import LOG_MODULE_USER, ROLE_ADMIN
from apps.api.core.security import require_role
from apps.api.models.user import User
from apps.api.routes.logs import write_log
from apps.api.schemas.user import UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=dict)
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    role: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(User)
    if keyword:
        stmt = stmt.where(User.username.contains(keyword) | User.nickname.contains(keyword))
    if role:
        stmt = stmt.where(User.role == role)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(User.id).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [UserOut.from_user(u).model_dump() for u in items],
    }


@router.post("", status_code=201)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, f"用户名 '{body.username}' 已存在")

    user = User(
        username=body.username,
        nickname=body.nickname,
        role=body.role,
        email=body.email,
        phone=body.phone,
    )
    user.set_password(body.password)
    db.add(user)
    db.commit()
    db.refresh(user)
    write_log(db, user=current_user["sub"], module=LOG_MODULE_USER, action="新增用户", target=body.username)
    return UserOut.from_user(user).model_dump()


@router.put("/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "password":
            if value:
                user.set_password(value)
        else:
            setattr(user, field, value)

    db.commit()
    db.refresh(user)
    write_log(db, user=current_user["sub"], module=LOG_MODULE_USER, action="编辑用户", target=user.username)
    return UserOut.from_user(user).model_dump()


@router.patch("/{user_id}/status")
def toggle_status(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    user.status = "停用" if user.status == "启用" else "启用"
    db.commit()
    db.refresh(user)
    write_log(db, user=current_user["sub"], module=LOG_MODULE_USER, action=f"{user.status}账号", target=user.username)
    return UserOut.from_user(user).model_dump()


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(ROLE_ADMIN)),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.username == "admin":
        raise HTTPException(400, "不能删除内置管理员账号")

    username = user.username
    db.delete(user)
    db.commit()
    write_log(db, user=current_user["sub"], module=LOG_MODULE_USER, action="删除用户", target=username)
