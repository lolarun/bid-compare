"""Minimal JWT auth — single admin account."""
import os
import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

try:
    import jwt as pyjwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_SECRET = os.getenv("JWT_SECRET", "mempas-dev-secret-change-in-prod")
_ALGORITHM = "HS256"
_ADMIN_USER = os.getenv("ADMIN_USER", "admin")
_ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest):
    if body.username != _ADMIN_USER or body.password != _ADMIN_PASS:
        raise HTTPException(401, "用户名或密码错误")

    if not _JWT_AVAILABLE:
        # fallback: return a simple static token when pyjwt not installed
        token = f"static-{body.username}"
    else:
        payload = {
            "sub": body.username,
            "role": "管理员",
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12),
        }
        token = pyjwt.encode(payload, _SECRET, algorithm=_ALGORITHM)

    return {
        "access_token": token,
        "token_type": "bearer",
        "username": body.username,
        "role": "管理员",
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
