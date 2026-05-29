"""JWT auth — validates against users table."""

import os
import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models.user import User

try:
    import jwt as pyjwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_SECRET = os.getenv("JWT_SECRET", "mempas-dev-secret-change-in-prod")
_ALGORITHM = "HS256"
_DEFAULT_ADMIN_USER = os.getenv("ADMIN_USER", "admin")
_DEFAULT_ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")


class LoginRequest(BaseModel):
    username: str
    password: str


def _ensure_admin(db: Session) -> None:
    """Seed the default admin account if users table is empty."""
    if db.query(User).count() > 0:
        return
    admin = User(
        username=_DEFAULT_ADMIN_USER,
        nickname="管理员",
        role="管理员",
    )
    admin.set_password(_DEFAULT_ADMIN_PASS)
    db.add(admin)
    db.commit()


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    _ensure_admin(db)

    user = db.query(User).filter(User.username == body.username).first()
    if not user or not user.verify_password(body.password):
        raise HTTPException(401, "用户名或密码错误")

    if user.status != "启用":
        raise HTTPException(403, "账号已停用，请联系管理员")

    user.last_login = datetime.datetime.now(datetime.timezone.utc)
    db.commit()

    if not _JWT_AVAILABLE:
        token = f"static-{user.username}"
    else:
        payload = {
            "sub": user.username,
            "role": user.role,
            "user_id": user.id,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12),
        }
        token = pyjwt.encode(payload, _SECRET, algorithm=_ALGORITHM)

    from apps.api.routes.logs import write_log
    write_log(db, user=user.username, module="系统", action="登录", target=user.username)

    return {
        "access_token": token,
        "token_type": "bearer",
        "username": user.username,
        "role": user.role,
    }


def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """Optional auth dependency — returns payload or raises 401."""
    if not cred:
        raise HTTPException(401, "未登录")
    if not _JWT_AVAILABLE:
        if cred.credentials.startswith("static-"):
            return {"sub": cred.credentials[7:], "role": "管理员"}
        raise HTTPException(401, "Token 无效")
    try:
        return pyjwt.decode(cred.credentials, _SECRET, algorithms=[_ALGORITHM])
    except Exception:
        raise HTTPException(401, "Token 无效或已过期")
