import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAppStore } from '../stores/app'

vi.mock('../api', () => ({
  analysisApi: {
    dashboard: vi.fn().mockResolvedValue({
      data: {
        total_materials: 100,
        total_suppliers: 10,
        total_projects: 5,
        total_quotes: 500,
        category_stats: [
          {
            category: '桥架',
            profession: '电气',
            total_materials: 20,
            total_quotes: 100,
            avg_price: 50.0,
            price_cv: 0.3,
            supplier_count: 5,
            project_count: 3,
          },
        ],
      },
    }),
  },
}))

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('useAppStore', () => {
  it('initial state', () => {
    const store = useAppStore()
    expect(store.collapsed).toBe(false)
    expect(store.dashboardData).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('toggleCollapsed', () => {
    const store = useAppStore()
    store.toggleCollapsed()
    expect(store.collapsed).toBe(true)
    store.toggleCollapsed()
    expect(store.collapsed).toBe(false)
  })

  it('fetchDashboard loads data', async () => {
    const store = useAppStore()
    await store.fetchDashboard()
    expect(store.dashboardData).not.toBeNull()
    expect(store.dashboardData!.total_materials).toBe(100)
    expect(store.dashboardData!.category_stats).toHaveLength(1)
    expect(store.dashboardData!.category_stats[0].category).toBe('桥架')
    expect(store.loading).toBe(false)
  })
})
