import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import NProgress from 'nprogress'
import 'nprogress/nprogress.css'
import BasicLayout from '@/layouts/BasicLayout.vue'
import { useUserStore } from '@/stores/user'
import type { Role } from '@/types/role'

NProgress.configure({ showSpinner: false })

/** Routes rendered inside BasicLayout (sidebar menu)
 *  group meta drives sidebar grouping; icon is an @ant-design/icons-vue component name.
 *  roles meta restricts access to specific roles (undefined = all roles). */
const appRoutes: RouteRecordRaw[] = [
  // ─── 工作台 ────────────────────────────────────────────────────────────
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('@/views/dashboard/IndexView.vue'),
    meta: { title: '仪表盘', icon: 'DashboardOutlined', group: '工作台' },
  },
  {
    path: '/queue',
    name: 'Queue',
    component: () => import('@/views/queue/IndexView.vue'),
    meta: { title: '数据流', icon: 'UnorderedListOutlined', group: '工作台' },
  },
  // ─── 业务功能 ──────────────────────────────────────────────────────────
  {
    path: '/invite',
    name: 'Invite',
    component: () => import('@/views/invite/IndexView.vue'),
    meta: { title: '邀标建议', icon: 'SolutionOutlined', group: '业务功能', roles: ['管理员', '比价员'] as Role[] },
  },
  {
    path: '/compare',
    name: 'Compare',
    component: () => import('@/views/compare/IndexView.vue'),
    meta: { title: '招标比价分析', icon: 'LineChartOutlined', group: '业务功能', roles: ['管理员', '比价员'] as Role[] },
  },
  // ─── 数据管理 ──────────────────────────────────────────────────────────
  {
    path: '/materials',
    name: 'Materials',
    component: () => import('@/views/materials/IndexView.vue'),
    meta: { title: '物料主数据', icon: 'AppstoreOutlined', group: '数据管理' },
  },
  {
    path: '/brand-tiers',
    name: 'BrandTiers',
    component: () => import('@/views/system/BrandTiersView.vue'),
    meta: { title: '品牌档位维护', icon: 'TagsOutlined', group: '数据管理', roles: ['管理员', '比价员'] as Role[] },
  },
  {
    path: '/analysis',
    name: 'DataAnalysis',
    component: () => import('@/views/history/IndexView.vue'),
    meta: { title: '历史价格查询', icon: 'FieldTimeOutlined', group: '数据管理' },
  },
  {
    path: '/projects',
    name: 'Projects',
    component: () => import('@/views/projects/IndexView.vue'),
    meta: { title: '项目管理', icon: 'ProjectOutlined', group: '数据管理' },
  },
  {
    path: '/suppliers',
    name: 'Suppliers',
    component: () => import('@/views/suppliers/IndexView.vue'),
    meta: { title: '供应商管理', icon: 'TeamOutlined', group: '数据管理' },
  },
  {
    path: '/import',
    name: 'Import',
    component: () => import('@/views/import/IndexView.vue'),
    meta: { title: '采购价格导入', icon: 'CloudUploadOutlined', group: '数据管理', roles: ['管理员', '比价员'] as Role[] },
  },
  {
    path: '/batches',
    name: 'Batches',
    component: () => import('@/views/batches/IndexView.vue'),
    meta: { title: '清单管理', icon: 'ContainerOutlined', group: '数据管理', roles: ['管理员', '比价员'] as Role[] },
  },
  // ─── 系统管理 ──────────────────────────────────────────────────────────
  {
    path: '/system/users',
    name: 'SystemUsers',
    component: () => import('@/views/system/UsersView.vue'),
    meta: { title: '用户管理', icon: 'UserOutlined', group: '系统管理', roles: ['管理员'] as Role[] },
  },
  {
    path: '/system/logs',
    name: 'SystemLogs',
    component: () => import('@/views/system/LogsView.vue'),
    meta: { title: '操作日志', icon: 'FileSearchOutlined', group: '系统管理', roles: ['管理员'] as Role[] },
  },
  {
    path: '/system/settings',
    name: 'SystemSettings',
    component: () => import('@/views/system/SettingsView.vue'),
    meta: { title: '系统设置', icon: 'SettingOutlined', group: '系统管理', roles: ['管理员'] as Role[] },
  },
]

const routes: RouteRecordRaw[] = [
  {
    path: '/help',
    name: 'Help',
    component: () => import('@/views/help/IndexView.vue'),
    meta: { title: '帮助中心', public: true },
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/login/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/403',
    name: 'Forbidden',
    component: () => import('@/views/exception/403.vue'),
    meta: { title: '无权限', public: true },
  },
  {
    path: '/',
    name: 'Layout',
    component: BasicLayout,
    redirect: '/dashboard',
    children: [
      ...appRoutes,
      // 比价向导深链：/compare/:projectId/:step? —— 刷新可恢复（不进侧边菜单，复用同组件）。
      {
        path: '/compare/:projectId/:step?',
        name: 'CompareDeep',
        component: () => import('@/views/compare/IndexView.vue'),
        // R1 止血：注释一直说"不进侧边菜单"，但漏了 group 只是让 SiderMenu 把它
        // 归进兜底的"其他"组显示出来，不是真正隐藏——菜单里"招标比价分析"重复
        // 两次正是这个原因。SiderMenu 已有 meta.hideInMenu 这个专门机制，用错了
        // 手段。
        meta: { title: '招标比价分析', public: false, hideInMenu: true, roles: ['管理员', '比价员'] as Role[] },
      },
      // Legacy path redirects — inside layout so auth guard runs before redirect
      { path: '/quotes', redirect: '/analysis' },
      { path: '/history', redirect: '/analysis' },
      { path: '/settings', redirect: '/system/settings' },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/exception/404.vue'),
    meta: { title: '404', public: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/* ── Route guard: auth + role check ─────────────────────────────────── */
const whiteList = ['Login', 'NotFound', 'Forbidden']
let refreshedToken = ''

router.beforeEach(async (to) => {
  NProgress.start()
  document.title = `${to.meta?.title || ''} - MEMPAS`.replace(/^ - /, '')

  const token = localStorage.getItem('mempas_token')

  // 1. Auth check: redirect to login if no token
  if (!token && !whiteList.includes(to.name as string) && !to.meta?.public) {
    return { name: 'Login', query: { redirect: encodeURIComponent(to.fullPath) } }
  }

  // Refresh role data once per browser token before evaluating route access.
  // Server-side RBAC remains authoritative; this prevents stale localStorage
  // from showing routes after an administrator changes a user's role.
  const userStore = useUserStore()
  if (token && token !== refreshedToken) {
    refreshedToken = token
    await userStore.fetchMe()
    if (!userStore.isLoggedIn()) {
      refreshedToken = ''
      return { name: 'Login', query: { redirect: encodeURIComponent(to.fullPath) } }
    }
  }

  // 2. Role check: verify user has required role for this route
  const requiredRoles = to.meta?.roles as Role[] | undefined
  if (requiredRoles && requiredRoles.length > 0) {
    const userRole = userStore.userInfo?.role
    if (userRole && !requiredRoles.includes(userRole as Role)) {
      return { name: 'Forbidden' }
    }
  }

  return true
})

router.afterEach(() => {
  NProgress.done()
})

export default router
export { appRoutes }
