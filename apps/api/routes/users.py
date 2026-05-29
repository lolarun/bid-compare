"""User management CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models.user import User
from apps.api.routes.auth import get_current_user
from apps.api.routes.logs import write_log
from apps.api.schemas.user import UserCreate, UserUpdate, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=dict)
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    role: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(User)
    if keyword:
        q = q.filter(User.username.contains(keyword) | User.nickname.contains(keyword))
    if role:
        q = q.filter(User.role == role)

    total = q.count()
    items = q.order_by(User.id).offset((page - 1) * page_size).limit(page_size).all()
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
    current_user: dict = Depends(get_current_user),
):
    if db.query(User).filter(User.username == body.username).first():
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
    write_log(db, user=current_user["sub"], module="用户管理", action="新增用户", target=body.username)
    return UserOut.from_user(user).model_dump()


@router.put("/{user_id}")
def update_user(user_id: int, body: UserUpdate, db: Session = Depends(get_db)):
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
    return UserOut.from_user(user).model_dump()


@router.patch("/{user_id}/status")
def toggle_status(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    user.status = "停用" if user.status == "启用" else "启用"
    db.commit()
    db.refresh(user)
    write_log(db, user=current_user["sub"], module="用户管理", action=f"{user.status}账号", target=user.username)
    return UserOut.from_user(user).model_dump()


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.username == "admin":
        raise HTTPException(400, "不能删除内置管理员账号")

    username = user.username
    db.delete(user)
    db.commit()
    write_log(db, user=current_user["sub"], module="用户管理", action="删除用户", target=username)
