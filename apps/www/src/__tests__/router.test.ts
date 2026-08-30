import { describe, it, expect } from 'vitest'
import router, { appRoutes } from '../router'

describe('Router', () => {
  it('has all expected app routes', () => {
    const paths = appRoutes.map((r) => r.path)
    expect(paths).toContain('/dashboard')
    // design/27 §10 步骤5：旧向导 /compare 退役，appRoutes 里不再有它的条目；
    // 新的"招标比价分析"入口是 /workspace/:projectId?，跟其他带路由参数的
    // 条目一样直接定义在 layout children 里（SiderMenu 从 router.getRoutes()
    // 读取实际菜单，不读 appRoutes——这个数组只覆盖无参数的简单项）。
    expect(paths).toContain('/invite')
    expect(paths).toContain('/materials')
    expect(paths).toContain('/analysis')
    expect(paths).toContain('/suppliers')
    expect(paths).toContain('/import')
    expect(paths).toContain('/system/users')
    expect(paths).toContain('/system/settings')
  })

  it('all app routes have meta.title', () => {
    for (const route of appRoutes) {
      expect(route.meta?.title).toBeTruthy()
    }
  })

  it('all app routes have lazy-loaded components', () => {
    for (const route of appRoutes) {
      expect(typeof route.component).toBe('function')
    }
  })

  it('all app routes have icon in meta', () => {
    for (const route of appRoutes) {
      expect(route.meta?.icon).toBeTruthy()
    }
  })
})

// design/45 §5.1 —— 进入项目先看只读的项目概述，三阶段工作台在 /compare 子路由。
// 这几条单独钉住：两个路径搞反了不会有类型错误，只会让用户点「进入比价」落到
// 一个只读页、或者点「返回工作台」把正在核查的矩阵丢掉。
describe('Project overview vs. workspace routing (design/45)', () => {
  const routes = router.getRoutes()
  const byName = (name: string) => routes.find((r) => r.name === name)

  it('/workspace/:projectId is the read-only project overview', () => {
    const r = byName('ProjectOverview')
    expect(r?.path).toBe('/workspace/:projectId')
    expect(r?.meta?.title).toBe('项目概述')
  })

  it('the three-stage workspace lives at the /compare child route', () => {
    const r = byName('CompareWorkspace')
    expect(r?.path).toBe('/workspace/:projectId/compare')
  })

  it('overview and workspace are distinct routes, not aliases', () => {
    expect(byName('ProjectOverview')?.path).not.toBe(byName('CompareWorkspace')?.path)
  })
})
