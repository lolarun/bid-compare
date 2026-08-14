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
  // design/27 §10 步骤5 —— 旧 5 步向导退役，此处不再放 appRoutes 条目；
  // 新工作台（下方 CompareWorkspace）是唯一的"招标比价分析"入口。旧路径
  // /compare、/compare/:projectId/:step? 保留为纯跳转（见下方 layout
  // children），不进侧边栏，只为旧书签/深链兜底。
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
      // design/27 §10 步骤5 —— 供应商主轴工作台是唯一的"招标比价分析"入口。
      // 旧向导（views/compare/IndexView.vue，5 步向导 + 8 芯片条 + 批量卡片流）
      // 已删除；下面两条旧路径改纯跳转，只为旧书签/深链兜底，不再挂组件。
      // 退役前置条件（2026-08-14）：真实 prj1/prj2 数据跑通"确认入库→
      // match→对齐核查→矩阵→导出"整条链路（含 checksum_ack、
      // missing_total_requires_review 两类阻断门禁的正确行为），见回归记录。
      {
        path: '/compare/:projectId/:step?',
        redirect: (to) => ({ path: `/workspace/${to.params.projectId ?? ''}` }),
      },
      {
        path: '/workspace/:projectId?',
        name: 'CompareWorkspace',
        component: () => import('@/views/compare/WorkspaceView.vue'),
        meta: {
          title: '招标比价分析', icon: 'LineChartOutlined', group: '业务功能',
          public: false, roles: ['管理员', '比价员'] as Role[],
        },
      },
      // design/27 §10 步骤4 —— 对齐核查独立视图（user decision D1），从工作台
      // 头部按钮进入，query 带 category/submission_ids（AnchorReviewMatrix 的
      // 必需 props，工作台已知这些值，不用再让用户选一遍）。
      {
        path: '/workspace/:projectId/align',
        name: 'CompareWorkspaceAlign',
        component: () => import('@/views/compare/AlignmentReviewView.vue'),
        meta: { title: '对齐核查', public: false, hideInMenu: true, roles: ['管理员', '比价员'] as Role[] },
      },
      // Legacy path redirects — inside layout so auth guard runs before redirect
      { path: '/compare', redirect: '/workspace' },
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
