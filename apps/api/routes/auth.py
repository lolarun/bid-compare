"""JWT auth — login, token validation, and current-user info."""

import os
import datetime

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.enums import ROLE_ADMIN
from apps.api.core.security import (
    get_current_user,
    create_access_token,
)
from apps.api.models.user import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_DEFAULT_ADMIN_USER = os.getenv("ADMIN_USER", "admin")
_DEFAULT_ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")


class LoginRequest(BaseModel):
    username: str
    password: str


def _ensure_admin(db: Session) -> None:
    """Seed the default admin account if users table is empty."""
    if db.scalar(select(func.count()).select_from(User)) > 0:
        return
    admin = User(
        username=_DEFAULT_ADMIN_USER,
        nickname="管理员",
        role=ROLE_ADMIN,
    )
    admin.set_password(_DEFAULT_ADMIN_PASS)
    db.add(admin)
    db.commit()


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    _ensure_admin(db)

    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not user.verify_password(body.password):
        raise HTTPException(401, "用户名或密码错误")

    if user.status != "启用":
        raise HTTPException(403, "账号已停用，请联系管理员")

    user.last_login = datetime.datetime.now(datetime.timezone.utc)
    db.commit()

    payload = {
        "sub": user.username,
        "role": user.role,
        "user_id": user.id,
    }
    token = create_access_token(payload)

    from apps.api.routes.logs import write_log
    write_log(db, user=user.username, module="系统", action="登录", target=user.username)

    return {
        "access_token": token,
        "token_type": "bearer",
        "username": user.username,
        "role": user.role,
        "nickname": user.nickname or user.username,
    }


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current user info from token — used by frontend to refresh user state."""
    user = db.scalar(select(User).where(User.username == current_user["sub"]))
    if not user:
        raise HTTPException(404, "用户不存在")
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname or user.username,
        "role": user.role,
        "email": user.email,
        "phone": user.phone,
        "status": user.status,
    }
