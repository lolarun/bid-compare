/**
 * 侧边导航的菜单数据源。侧栏（全部分组）与内容区面包屑读同一份路由推导结果，
 * 所以抽在这里——各算一遍角色过滤，迟早不一致。
 *
 * 2026-09-03 曾为"混合布局"提供过 `activeGroup`/`activeGroupItems`/`openGroup`
 * （一级分组进顶栏）；2026-09-04 用户决定导航全部回到左侧，那三个导出随之删除，
 * 需要时从 git 历史取回。
 *
 * 菜单始终由路由表推导（`router.getRoutes()`），没有第二份菜单配置：加一个页面
 * 只需在 router/index.ts 写 meta.title + meta.group。
 */
import { computed } from 'vue'
import { useRoute, useRouter, type RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'
import type { Role } from '@/types/role'

export interface MenuItem {
  /** 路由 path，同时用作 a-menu 的 key */
  key: string
  title: string
  icon?: string
  group: string
}

export interface MenuGroup {
  title: string
  items: MenuItem[]
}

const GROUP_ORDER = ['工作台', '业务功能', '数据管理', '系统管理'] as const

export function useAppMenu() {
  const router = useRouter()
  const route = useRoute()
  const userStore = useUserStore()

  function hasRouteAccess(routeRoles: unknown): boolean {
    if (!routeRoles || !Array.isArray(routeRoles) || routeRoles.length === 0) return true
    const userRole = userStore.userInfo?.role
    if (!userRole) return false
    return (routeRoles as Role[]).includes(userRole as Role)
  }

  function collectLeafRoutes(routes: readonly RouteRecordRaw[]): RouteRecordRaw[] {
    const out: RouteRecordRaw[] = []
    for (const r of routes) {
      if (r.children && r.children.length > 0) out.push(...collectLeafRoutes(r.children))
      else out.push(r)
    }
    return out
  }

  const groups = computed<MenuGroup[]>(() => {
    const layoutRoute = router.getRoutes().find((r) => r.name === 'Layout')
    if (!layoutRoute) return []
    const children = collectLeafRoutes(layoutRoute.children ?? [])
    const map = new Map<string, MenuItem[]>()
    for (const r of children) {
      if (!r.meta?.title || r.meta?.hideInMenu) continue
      if (!hasRouteAccess(r.meta.roles)) continue
      const group = (r.meta.group as string) || '其他'
      const path = r.path.startsWith('/') ? r.path : `/${r.path}`
      const list = map.get(group) ?? []
      list.push({ key: path, title: r.meta.title as string, icon: r.meta.icon as string | undefined, group })
      map.set(group, list)
    }
    const rest = [...map.keys()].filter(
      (g) => !GROUP_ORDER.includes(g as (typeof GROUP_ORDER)[number]),
    )
    return [...GROUP_ORDER, ...rest]
      .filter((g) => map.has(g))
      .map((g) => ({ title: g, items: map.get(g)! }))
  })

  /** 当前路径命中的菜单项：取 key 最长的前缀匹配。
   *
   *  精确匹配 route.path 不够用——`hideInMenu` 的深层页（项目概述
   *  `/workspace/:projectId`、比价工作台 `/workspace/:projectId/compare`）在菜单里
   *  没有自己的条目，精确匹配会让侧栏一项都不高亮。
   *  前缀匹配把它们归到父入口（`/workspace` → 招标比价）名下。 */
  const activeItem = computed<MenuItem | null>(() => {
    const all = groups.value.flatMap((g) => g.items)
    const hit = all
      .filter((it) => route.path === it.key || route.path.startsWith(`${it.key}/`))
      .sort((a, b) => b.key.length - a.key.length)[0]
    return hit ?? null
  })

  const selectedKeys = computed<string[]>(() => [activeItem.value?.key ?? route.path])

  return { groups, activeItem, selectedKeys }
}
