import { describe, it, expect } from 'vitest'
import { appRoutes } from '../router'

describe('Router', () => {
  it('has all expected app routes', () => {
    const paths = appRoutes.map((r) => r.path)
    expect(paths).toContain('/dashboard')
    expect(paths).toContain('/compare')
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
