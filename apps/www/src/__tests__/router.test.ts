import { describe, it, expect } from 'vitest'
import { appRoutes } from '../router'

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
