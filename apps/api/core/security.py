"""Security module — password hashing, JWT tokens, and RBAC dependencies.

Centralizes all auth primitives so routes and models import from one place.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import secrets
from typing import Any

import jwt as pyjwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.core.enums import ROLE_ADMIN

_SECRET = os.getenv("JWT_SECRET", "mempas-dev-secret-change-in-prod")
_ALGORITHM = "HS256"
_DEFAULT_EXPIRY_HOURS = 12

_bearer = HTTPBearer(auto_error=False)


# ── Password hashing ─────────────────────────────────────────────────────────

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Hash a password with PBKDF2-HMAC-SHA256 (260k iterations).

    Returns (hash_hex, salt_hex).
    """
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return h.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """Verify a password against the stored hash and salt."""
    h, _ = hash_password(password, salt)
    return h == expected_hash


# ── JWT tokens ───────────────────────────────────────────────────────────────

def create_access_token(payload: dict[str, Any], expires_hours: int = _DEFAULT_EXPIRY_HOURS) -> str:
    """Create a signed JWT access token."""
    data = {
        **payload,
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=expires_hours),
    }
    return pyjwt.encode(data, _SECRET, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT token. Returns the payload dict.

    Raises HTTPException(401) on invalid/expired tokens.
    """
    try:
        return pyjwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except Exception:
        raise HTTPException(401, "Token 无效或已过期")


# ── FastAPI dependencies ─────────────────────────────────────────────────────

def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict[str, Any]:
    """Auth dependency — validates bearer token and returns the payload.

    Returns dict with keys: sub (username), role, user_id.
    Raises 401 if no token or invalid token.
    """
    if not cred:
        raise HTTPException(401, "未登录")
    return decode_access_token(cred.credentials)


def require_role(*roles: str):
    """Factory: returns a FastAPI dependency that checks the current user's role.

    Usage:
        @router.post("/users", dependencies=[Depends(require_role(ROLE_ADMIN))])
        def create_user(...): ...

    Or as a function parameter:
        def create_user(current_user: dict = Depends(require_role(ROLE_ADMIN))): ...
    """
    allowed = set(roles)

    def _check_role(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in allowed:
            raise HTTPException(403, "权限不足，无法执行此操作")
        return current_user

    return _check_role


# Convenience: admin-only dependency
require_admin = require_role(ROLE_ADMIN)
