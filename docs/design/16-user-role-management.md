# 16 — User & Role Management Design

> Status: **Active** · Created: 2026-07-14 · Author: system

## 1. Goal

Provide role-based access control (RBAC) for MEMPAS using the existing three-role
system (管理员 / 比价员 / 查看者). No new database tables — roles are enforced via a
reusable FastAPI dependency that checks the role string in the JWT payload.

## 2. Role Definitions

| Constant | Display | Description |
|---|---|---|
| `ROLE_ADMIN` | 管理员 | Full system access — user management, system settings, logs, all business operations |
| `ROLE_BUYER` | 比价员 | Business operations — invite, compare, import, data management (edit), export |
| `ROLE_VIEWER` | 查看者 | Read-only — dashboard, materials/suppliers/projects (view only) |

## 3. Permission Matrix

| Capability | 管理员 | 比价员 | 查看者 |
|---|---|---|---|
| Dashboard / Queue (view) | Y | Y | Y |
| Invite (邀标建议) | Y | Y | N |
| Compare (招标比价分析) | Y | Y | N |
| Materials / Suppliers / Projects (view) | Y | Y | Y |
| Materials / Suppliers / Projects (edit) | Y | Y | N |
| Import (采购价格导入) | Y | Y | N |
| Batches (清单管理) | Y | Y | N |
| User Management | Y | N | N |
| System Logs | Y | N | N |
| System Settings | Y | N | N |
| Export (Excel) | Y | Y | N |

## 4. Backend Design

### 4.1 Security Module (`apps/api/core/security.py`)

Centralizes all auth primitives so routes and models import from one place.

**Functions:**
- `hash_password(password: str) -> tuple[str, str]` — PBKDF2-HMAC-SHA256, 260k iterations
- `verify_password(password: str, salt: str, expected_hash: str) -> bool`
- `create_access_token(payload: dict, expires_hours: int = 12) -> str` — JWT HS256
- `decode_access_token(token: str) -> dict`
- `get_current_user(cred)` — FastAPI dependency, returns `{"sub", "role", "user_id"}`
- `require_role(*roles: str)` — factory returning a dependency that checks role membership
- `require_admin` — convenience alias for `require_role(ROLE_ADMIN)`

**JWT payload:**
```json
{
  "sub": "username",
  "role": "管理员",
  "user_id": 1,
  "exp": 1234567890
}
```

### 4.2 Endpoint Authorization Rules

All non-auth routers already have `Depends(get_current_user)` at the app level (authentication).
Per-route `require_role` dependencies add the authorization layer on top.

| Route | Method | Required Role |
|---|---|---|
| `/api/auth/login` | POST | public (no auth) |
| `/api/auth/me` | GET | any authenticated |
| `/api/users` | GET | any authenticated |
| `/api/users` | POST | 管理员 |
| `/api/users/{id}` | PUT | 管理员 |
| `/api/users/{id}/status` | PATCH | 管理员 |
| `/api/users/{id}` | DELETE | 管理员 |
| `/api/config/{key}` | PUT | 管理员 |
| `/api/logs` | GET | 管理员 |
| `/api/export/*` | GET | 管理员, 比价员 |
| all other routes | * | any authenticated |

### 4.3 Default Admin Seeding

On first login when the users table is empty, `_ensure_admin()` seeds:
- Username: `ADMIN_USER` env (default `admin`)
- Password: `ADMIN_PASS` env (default `admin123`)
- Role: `管理员`

## 5. Frontend Design

### 5.1 Route Meta

Routes that need role restriction carry `meta.roles: Role[]`:
```typescript
meta: { title: '用户管理', icon: 'UserOutlined', group: '系统管理', roles: ['管理员'] }
```

### 5.2 Route Guard

`router.beforeEach` checks:
1. Token presence (existing behavior)
2. `meta.roles` against `userStore.userInfo?.role` — redirect to `/403` if insufficient

### 5.3 SiderMenu Filtering

Menu items are filtered: if `meta.roles` exists and doesn't include the current user's role,
the item is hidden from the sidebar.

### 5.4 HeaderView

The non-functional role selector is replaced with a read-only tag showing the user's actual role:
- 管理员 → red tag
- 比价员 → blue tag
- 查看者 → default tag

## 6. Migration Notes

- **No database migration needed** — the `users.role` column already exists as `String(16)`.
- **No breaking change to existing tokens** — JWT payload structure is unchanged.
- **pyjwt** is already installed (2.12.1) but not declared in `pyproject.toml` — this is fixed.
- The `PUT /api/users/{id}` endpoint gains `current_user` dependency — previously any authenticated
  user could update any user without audit logging. This is a security fix.
