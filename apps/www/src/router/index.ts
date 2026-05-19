import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import NProgress from 'nprogress'
import 'nprogress/nprogress.css'
import BasicLayout from '@/layouts/BasicLayout.vue'

NProgress.configure({ showSpinner: false })

/** Routes rendered inside BasicLayout (sidebar menu)
 *  group meta drives sidebar grouping; icon is an @ant-design/icons-vue component name. */
const appRoutes: RouteRecordRaw[] = [
  // ─── 工作台 ────────────────────────────────────────────────────────────
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('@/views/dashboard/IndexView.vue'),
    meta: { title: '仪表盘', icon: 'DashboardOutlined', group: '工作台' },
  },
  // ─── 业务功能 ──────────────────────────────────────────────────────────
  {
    path: '/compare',
    name: 'Compare',
    component: () => import('@/views/compare/IndexView.vue'),
    meta: { title: '招标比价分析', icon: 'LineChartOutlined', group: '业务功能' },
  },
  {
    path: '/invite',
    name: 'Invite',
    component: () => import('@/views/invite/IndexView.vue'),
    meta: { title: '邀标建议', icon: 'SolutionOutlined', group: '业务功能' },
  },
  // ─── 数据管理 ──────────────────────────────────────────────────────────
  {
    path: '/materials',
    name: 'Materials',
    component: () => import('@/views/materials/IndexView.vue'),
    meta: { title: '物料主数据', icon: 'AppstoreOutlined', group: '数据管理' },
  },
  {
    path: '/analysis',
    name: 'DataAnalysis',
    component: () => import('@/views/history/IndexView.vue'),
    meta: { title: '采购数据分析', icon: 'FieldTimeOutlined', group: '数据管理' },
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
    meta: { title: '采购价格导入', icon: 'CloudUploadOutlined', group: '数据管理' },
  },
  // ─── 系统管理 ──────────────────────────────────────────────────────────
  {
    path: '/system/users',
    name: 'SystemUsers',
    component: () => import('@/views/system/UsersView.vue'),
    meta: { title: '用户管理', icon: 'UserOutlined', group: '系统管理' },
  },
  {
    path: '/system/logs',
    name: 'SystemLogs',
    component: () => import('@/views/system/LogsView.vue'),
    meta: { title: '操作日志', icon: 'FileSearchOutlined', group: '系统管理' },
  },
  {
    path: '/system/settings',
    name: 'SystemSettings',
    component: () => import('@/views/system/SettingsView.vue'),
    meta: { title: '系统设置', icon: 'SettingOutlined', group: '系统管理' },
  },
]

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/login/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/',
    name: 'Layout',
    component: BasicLayout,
    redirect: '/dashboard',
    children: [
      ...appRoutes,
      // Legacy path redirects — inside layout so auth guard runs before redirect
      { path: '/projects', redirect: '/compare' },
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

/* ── Route guard: auth check ─────────────────────────────────────────── */
const whiteList = ['Login', 'NotFound']

router.beforeEach((to) => {
  NProgress.start()
  document.title = `${to.meta?.title || ''} - MEMPAS`.replace(/^ - /, '')

  const token = localStorage.getItem('mempas_token')
  if (!token && !whiteList.includes(to.name as string) && !to.meta?.public) {
    return { name: 'Login', query: { redirect: encodeURIComponent(to.fullPath) } }
  }
  return true
})

router.afterEach(() => {
  NProgress.done()
})

export default router
export { appRoutes }
