"""Unit tests for authentication and RBAC (Role-Based Access Control).

Tests cover:
1. Password hashing / verification
2. JWT token creation / decoding
3. require_role dependency behavior
4. Login endpoint (success, wrong password, disabled account)
5. GET /api/auth/me endpoint
6. User CRUD with RBAC (admin / buyer / viewer)
7. Edge cases (duplicate username, delete built-in admin)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.core import database as db_mod
from apps.api.core.enums import ROLE_ADMIN, ROLE_BUYER, ROLE_VIEWER
from apps.api.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from apps.api.models.user import User

# ── Test fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def temp_engine(tmp_path, monkeypatch):
    """Create an isolated SQLite DB and monkeypatch the database module."""
    db_path = tmp_path / "test_rbac.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", SessionLocal)

    # Ensure all models are loaded
    from apps.api.models import (  # noqa: F401
        AnalysisConfig,
        BidInvitation,
        BrandTier,
        ExtractionJob,
        Material,
        Project,
        Quote,
        Supplier,
        TenderDocument,
    )
    db_mod.Base.metadata.create_all(bind=engine)
    return engine, SessionLocal


@pytest.fixture
def client(temp_engine, monkeypatch):
    """Create a TestClient with the temp DB, bypassing the lifespan startup."""
    from apps.api.main import app

    _, SessionLocal = temp_engine

    # Override get_db to use the temp session
    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from apps.api.core.database import get_db
    app.dependency_overrides[get_db] = _get_db

    # We need to bypass the lifespan (which calls init_db and builds the pipeline)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()


def _create_user(db, username: str, password: str, role: str, nickname: str = "", status: str = "启用"):
    """Helper: create a user directly in the DB."""
    user = User(username=username, nickname=nickname or username, role=role, status=status)
    user.set_password(password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str, password: str):
    """Helper: login and return the token."""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return resp


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 1. Password hashing tests ────────────────────────────────────────────────

class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        h, salt = hash_password("mypassword123")
        assert verify_password("mypassword123", salt, h)

    def test_wrong_password_fails(self):
        h, salt = hash_password("correct")
        assert not verify_password("wrong", salt, h)

    def test_different_salts_produce_different_hashes(self):
        h1, s1 = hash_password("same")
        h2, s2 = hash_password("same")
        assert s1 != s2  # random salt
        assert h1 != h2  # different salt → different hash
        # Both should verify correctly
        assert verify_password("same", s1, h1)
        assert verify_password("same", s2, h2)


# ── 2. JWT token tests ───────────────────────────────────────────────────────

class TestJWTokens:
    def test_create_and_decode_token(self):
        payload = {"sub": "testuser", "role": ROLE_ADMIN, "user_id": 1}
        token = create_access_token(payload)
        decoded = decode_access_token(token)
        assert decoded["sub"] == "testuser"
        assert decoded["role"] == ROLE_ADMIN
        assert decoded["user_id"] == 1
        assert "exp" in decoded

    def test_decode_invalid_token_raises_401(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            decode_access_token("invalid.token.here")
        assert exc_info.value.status_code == 401


# ── 3. Login endpoint tests ──────────────────────────────────────────────────

class TestLogin:
    def test_login_success(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        _create_user(db, "admin", "admin123", ROLE_ADMIN, nickname="管理员")
        db.close()

        resp = _login(client, "admin", "admin123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["token_type"] == "bearer"
        assert data["username"] == "admin"
        assert data["role"] == ROLE_ADMIN
        assert data["nickname"] == "管理员"

    def test_login_wrong_password(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        _create_user(db, "admin", "admin123", ROLE_ADMIN)
        db.close()

        resp = _login(client, "admin", "wrongpassword")
        assert resp.status_code == 401
        assert "用户名或密码错误" in resp.json()["detail"]

    def test_login_nonexistent_user(self, client, temp_engine):
        resp = _login(client, "ghost", "whatever")
        assert resp.status_code == 401

    def test_login_disabled_account(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        _create_user(db, "disabled", "pass123", ROLE_BUYER, status="停用")
        db.close()

        resp = _login(client, "disabled", "pass123")
        assert resp.status_code == 403
        assert "停用" in resp.json()["detail"]

    def test_login_seeds_default_admin(self, client, temp_engine):
        """When users table is empty, login should seed the default admin."""
        resp = _login(client, "admin", "admin123")
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"
        assert resp.json()["role"] == ROLE_ADMIN


# ── 4. GET /api/auth/me tests ────────────────────────────────────────────────

class TestAuthMe:
    def test_me_returns_user_info(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        _create_user(db, "admin", "admin123", ROLE_ADMIN, nickname="管理员")
        db.close()

        resp = _login(client, "admin", "admin123")
        token = resp.json()["access_token"]

        resp = client.get("/api/auth/me", headers=_auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == ROLE_ADMIN
        assert data["nickname"] == "管理员"

    def test_me_without_token_returns_401(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401


# ── 5. User CRUD with RBAC tests ─────────────────────────────────────────────

class TestUserCRUD_RBAC:
    """Test that role-based access control is enforced on user management endpoints."""

    def _setup_users(self, db):
        """Create admin, buyer, and viewer users. Returns dict of username → id."""
        admin = _create_user(db, "admin", "admin123", ROLE_ADMIN, nickname="管理员")
        buyer = _create_user(db, "buyer1", "buyer123", ROLE_BUYER, nickname="比价员1")
        viewer = _create_user(db, "viewer1", "viewer123", ROLE_VIEWER, nickname="查看者1")
        return {"admin": admin.id, "buyer1": buyer.id, "viewer1": viewer.id}

    def _get_token(self, client, username, password):
        resp = _login(client, username, password)
        return resp.json()["access_token"]

    def test_admin_can_list_users(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.get("/api/users", headers=_auth_header(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    def test_buyer_can_list_users(self, client, temp_engine):
        """All authenticated users can view the user list."""
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.get("/api/users", headers=_auth_header(token))
        assert resp.status_code == 200

    def test_admin_can_create_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.post("/api/users", headers=_auth_header(token), json={
            "username": "newuser",
            "password": "newpass123",
            "nickname": "新用户",
            "role": ROLE_BUYER,
        })
        assert resp.status_code == 201
        assert resp.json()["username"] == "newuser"
        assert resp.json()["role"] == ROLE_BUYER

    def test_buyer_cannot_create_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.post("/api/users", headers=_auth_header(token), json={
            "username": "newuser",
            "password": "newpass123",
            "role": ROLE_BUYER,
        })
        assert resp.status_code == 403

    def test_viewer_cannot_create_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "viewer1", "viewer123")
        resp = client.post("/api/users", headers=_auth_header(token), json={
            "username": "newuser",
            "password": "newpass123",
            "role": ROLE_VIEWER,
        })
        assert resp.status_code == 403

    def test_admin_can_update_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.put(f"/api/users/{ids['buyer1']}", headers=_auth_header(token), json={
            "nickname": "更新昵称",
            "email": "test@example.com",
        })
        assert resp.status_code == 200
        assert resp.json()["nickname"] == "更新昵称"
        assert resp.json()["email"] == "test@example.com"

    def test_buyer_cannot_update_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.put(f"/api/users/{ids['admin']}", headers=_auth_header(token), json={
            "nickname": "hacked",
        })
        assert resp.status_code == 403

    def test_admin_can_toggle_status(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.patch(f"/api/users/{ids['buyer1']}/status", headers=_auth_header(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "停用"

    def test_buyer_cannot_toggle_status(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.patch(f"/api/users/{ids['admin']}/status", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_admin_can_delete_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.delete(f"/api/users/{ids['buyer1']}", headers=_auth_header(token))
        assert resp.status_code == 204

    def test_buyer_cannot_delete_user(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.delete(f"/api/users/{ids['admin']}", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_cannot_delete_builtin_admin(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        ids = self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.delete(f"/api/users/{ids['admin']}", headers=_auth_header(token))
        assert resp.status_code == 400
        assert "内置管理员" in resp.json()["detail"]

    def test_cannot_create_duplicate_username(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.post("/api/users", headers=_auth_header(token), json={
            "username": "buyer1",
            "password": "whatever",
            "role": ROLE_BUYER,
        })
        assert resp.status_code == 409

    def test_create_user_without_token_returns_401(self, client):
        resp = client.post("/api/users", json={
            "username": "hacker",
            "password": "hack",
            "role": ROLE_ADMIN,
        })
        assert resp.status_code == 401


# ── 6. RBAC on other endpoints ───────────────────────────────────────────────

class TestRBACOtherEndpoints:
    """Test role-based access on config, logs, and export endpoints."""

    def _setup_users(self, db):
        _create_user(db, "admin", "admin123", ROLE_ADMIN, nickname="管理员")
        _create_user(db, "buyer1", "buyer123", ROLE_BUYER, nickname="比价员")
        _create_user(db, "viewer1", "viewer123", ROLE_VIEWER, nickname="查看者")

    def _get_token(self, client, username, password):
        resp = _login(client, username, password)
        return resp.json()["access_token"]

    def test_viewer_cannot_access_logs(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "viewer1", "viewer123")
        resp = client.get("/api/logs", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_buyer_cannot_access_logs(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.get("/api/logs", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_admin_can_access_logs(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.get("/api/logs", headers=_auth_header(token))
        assert resp.status_code == 200

    def test_viewer_cannot_export(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "viewer1", "viewer123")
        resp = client.get("/api/export/dashboard", headers=_auth_header(token))
        assert resp.status_code == 403

    def test_buyer_can_export(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.get("/api/export/dashboard", headers=_auth_header(token))
        # 200 means the role check passed (may still error on data, but not 403)
        assert resp.status_code != 403

    def test_admin_can_export(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        db.close()

        token = self._get_token(client, "admin", "admin123")
        resp = client.get("/api/export/dashboard", headers=_auth_header(token))
        assert resp.status_code != 403

    def test_viewer_cannot_update_config(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        # Seed default config
        from apps.api.models import AnalysisConfig
        db.add(AnalysisConfig(key="scoring_weights", value={"a": 1.0}, description="test"))
        db.commit()
        db.close()

        token = self._get_token(client, "viewer1", "viewer123")
        resp = client.put("/api/config/scoring_weights", headers=_auth_header(token), json={
            "value": {"a": 0.5, "b": 0.5},
        })
        assert resp.status_code == 403

    def test_buyer_cannot_update_config(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        self._setup_users(db)
        from apps.api.models import AnalysisConfig
        db.add(AnalysisConfig(key="scoring_weights", value={"a": 1.0}, description="test"))
        db.commit()
        db.close()

        token = self._get_token(client, "buyer1", "buyer123")
        resp = client.put("/api/config/scoring_weights", headers=_auth_header(token), json={
            "value": {"a": 0.5, "b": 0.5},
        })
        assert resp.status_code == 403


# ── 8. Global auth dependency wiring (main.py) ───────────────────────────────
#
# 评审 G1：main.py 给除 auth 路由外的一切路由挂了全局鉴权依赖
# （`for router in all_routers: ... else: app.include_router(router,
# dependencies=[Depends(get_current_user)])`），但这条全局装配本身从未被独立
# 测过——已有的 401 用例（TestAuthMe.test_me_without_token_returns_401、
# TestUserCRUD_RBAC.test_create_user_without_token_returns_401）都长在
# /api/auth 或 /api/users 自己的路由文件里，测的是那个端点，不是全局装配。
# 且多个测试文件曾各自手写 `app.dependency_overrides[get_current_user] = ...`
# 从不清理（G1 的另一半），使得鉴权在实践中"总是绿的"，掩盖了这条全局装配
# 从未被验证过的事实。这里选一个与 auth/users 无关的路由（/api/suppliers）
# 直接验证装配本身：无 token 必须 401，有效 token 必须放行。
class TestGlobalAuthDependency:
    def test_unrelated_route_401s_without_token(self, client):
        """/api/suppliers 与 auth/users 路由无关，验证的是 main.py 的全局装配
        本身，不是某个路由自己写的鉴权检查。"""
        resp = client.get("/api/suppliers")
        assert resp.status_code == 401

    def test_unrelated_route_succeeds_with_valid_token(self, client, temp_engine):
        _, SessionLocal = temp_engine
        db = SessionLocal()
        _create_user(db, "admin", "admin123", ROLE_ADMIN, nickname="管理员")
        db.close()

        token = self._login_token(client, "admin", "admin123")
        resp = client.get("/api/suppliers", headers=_auth_header(token))
        assert resp.status_code == 200

    def test_auth_router_itself_is_not_gated(self, client):
        """/api/auth/login 必须不需要 token 就能访问——否则谁都登不进去。

        故意用不存在的用户名：无论如何都会 401，但来源必须是登录路由自己的
        "用户名或密码错误"，而不是全局鉴权依赖在进路由前就先拒绝了请求
        （那样 detail 会是 FastAPI 标准的 "Not authenticated"）。
        """
        resp = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        assert resp.status_code == 401
        assert "用户名或密码错误" in resp.json()["detail"]

    @staticmethod
    def _login_token(client, username, password):
        resp = _login(client, username, password)
        return resp.json()["access_token"]
