/**
 * Role types and permission helpers for MEMPAS RBAC.
 *
 * Three roles: 管理员 (admin), 比价员 (buyer), 查看者 (viewer).
 * Role strings must match the backend constants in apps/api/core/enums.py.
 */

export type Role = '管理员' | '比价员' | '查看者'

export const ROLE_ADMIN: Role = '管理员'
export const ROLE_BUYER: Role = '比价员'
export const ROLE_VIEWER: Role = '查看者'

/** Tag color per role for UI badges. */
export const ROLE_TAG_COLOR: Record<Role, string> = {
  '管理员': 'red',
  '比价员': 'blue',
  '查看者': 'default',
}
