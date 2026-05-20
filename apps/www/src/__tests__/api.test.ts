import { describe, it, expect, vi, beforeEach } from 'vitest'
import api from '../api/client'
import { materialApi, supplierApi, projectApi, quoteApi, analysisApi, configApi } from '../api'

vi.mock('../api/client', () => {
  const mockApi = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      response: { use: vi.fn() },
    },
    defaults: { baseURL: '/api' },
    create: vi.fn(),
  }
  return { default: mockApi }
})

const mockApi = api as unknown as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('materialApi', () => {
  it('list calls GET /materials', async () => {
    mockApi.get.mockResolvedValue({ data: { total: 1, items: [] } })
    await materialApi.list({ page: 1, page_size: 20 })
    expect(mockApi.get).toHaveBeenCalledWith('/materials', { params: { page: 1, page_size: 20 } })
  })

  it('get calls GET /materials/:id', async () => {
    mockApi.get.mockResolvedValue({ data: { id: 1 } })
    await materialApi.get(1)
    expect(mockApi.get).toHaveBeenCalledWith('/materials/1')
  })

  it('create calls POST /materials', async () => {
    const data = { standard_name: '蝶阀', category: '阀门', profession: '给排水' }
    mockApi.post.mockResolvedValue({ data: { id: 1, ...data } })
    await materialApi.create(data)
    expect(mockApi.post).toHaveBeenCalledWith('/materials', data)
  })

  it('update calls PUT /materials/:id', async () => {
    mockApi.put.mockResolvedValue({ data: { id: 1 } })
    await materialApi.update(1, { spec: 'DN100' })
    expect(mockApi.put).toHaveBeenCalledWith('/materials/1', { spec: 'DN100' })
  })

  it('delete calls DELETE /materials/:id', async () => {
    mockApi.delete.mockResolvedValue({})
    await materialApi.delete(1)
    expect(mockApi.delete).toHaveBeenCalledWith('/materials/1')
  })

  it('categories calls GET /materials/categories', async () => {
    mockApi.get.mockResolvedValue({ data: [] })
    await materialApi.categories()
    expect(mockApi.get).toHaveBeenCalledWith('/materials/categories')
  })

  it('standardize calls POST /materials/standardize', async () => {
    mockApi.post.mockResolvedValue({ data: { original: '热镀锌', standardized: '热浸镀锌', changes: ['热镀锌 → 热浸镀锌'] } })
    await materialApi.standardize({ text: '热镀锌', category: '桥架' })
    expect(mockApi.post).toHaveBeenCalledWith('/materials/standardize', { text: '热镀锌', category: '桥架' })
  })

  it('extendedSchema calls GET /materials/extended-schema/:category', async () => {
    mockApi.get.mockResolvedValue({ data: { category: '桥架', fields: [] } })
    await materialApi.extendedSchema('桥架')
    expect(mockApi.get).toHaveBeenCalledWith('/materials/extended-schema/桥架')
  })
})

describe('supplierApi', () => {
  it('list calls GET /suppliers', async () => {
    mockApi.get.mockResolvedValue({ data: { total: 0, items: [] } })
    await supplierApi.list()
    expect(mockApi.get).toHaveBeenCalledWith('/suppliers', { params: undefined })
  })

  it('create calls POST /suppliers', async () => {
    mockApi.post.mockResolvedValue({ data: { id: 1, name: '供A' } })
    await supplierApi.create({ name: '供A' })
    expect(mockApi.post).toHaveBeenCalledWith('/suppliers', { name: '供A' })
  })
})

describe('projectApi', () => {
  it('list calls GET /projects', async () => {
    mockApi.get.mockResolvedValue({ data: { total: 0, items: [] } })
    await projectApi.list()
    expect(mockApi.get).toHaveBeenCalledWith('/projects', { params: undefined })
  })
})

describe('quoteApi', () => {
  it('list with category filter', async () => {
    mockApi.get.mockResolvedValue({ data: { total: 0, items: [] } })
    await quoteApi.list({ category: '桥架' })
    expect(mockApi.get).toHaveBeenCalledWith('/quotes', { params: { category: '桥架' } })
  })

  it('stats calls GET /quotes/stats', async () => {
    mockApi.get.mockResolvedValue({ data: { total: 100, avg_price: 50.0 } })
    await quoteApi.stats({ category: '桥架' })
    expect(mockApi.get).toHaveBeenCalledWith('/quotes/stats', { params: { category: '桥架' } })
  })

  it('import calls POST /quotes/import', async () => {
    // Audit-fix: we no longer set Content-Type explicitly — axios attaches
    // the proper "multipart/form-data; boundary=..." when given FormData,
    // and explicit override stripped the boundary on some servers.
    const formData = new FormData()
    mockApi.post.mockResolvedValue({ data: { status: 'ok', imported: 5 } })
    await quoteApi.import(formData)
    expect(mockApi.post).toHaveBeenCalledWith('/quotes/import', formData, {})
  })
})

describe('analysisApi', () => {
  it('dashboard calls GET /analysis/dashboard', async () => {
    mockApi.get.mockResolvedValue({ data: { total_materials: 0 } })
    await analysisApi.dashboard()
    expect(mockApi.get).toHaveBeenCalledWith('/analysis/dashboard')
  })

  it('compare calls POST /analysis/compare with baseline_type', async () => {
    const req = { category: '桥架', new_price: 50, baseline_type: 'mean' }
    mockApi.post.mockResolvedValue({ data: { alert_level: 'green' } })
    await analysisApi.compare(req)
    expect(mockApi.post).toHaveBeenCalledWith('/analysis/compare', req)
  })

  it('supplierScore calls POST /analysis/supplier-score', async () => {
    mockApi.post.mockResolvedValue({ data: { total_score: 80 } })
    await analysisApi.supplierScore({ supplier_id: 1 })
    expect(mockApi.post).toHaveBeenCalledWith('/analysis/supplier-score', { supplier_id: 1 })
  })

  it('multiCompare calls POST /analysis/multi-compare', async () => {
    const req = { supplier_ids: [1, 2], category: '桥架' }
    mockApi.post.mockResolvedValue({ data: { category: '桥架', suppliers: [] } })
    await analysisApi.multiCompare(req)
    expect(mockApi.post).toHaveBeenCalledWith('/analysis/multi-compare', req)
  })

  it('categoryStats calls GET /analysis/category-stats/:category', async () => {
    mockApi.get.mockResolvedValue({ data: { category: '桥架', total_records: 100 } })
    await analysisApi.categoryStats('桥架')
    expect(mockApi.get).toHaveBeenCalledWith('/analysis/category-stats/桥架')
  })

  it('refreshBaselines calls POST with category param', async () => {
    mockApi.post.mockResolvedValue({ data: { status: 'ok' } })
    await analysisApi.refreshBaselines('阀门')
    expect(mockApi.post).toHaveBeenCalledWith('/analysis/refresh-baselines', null, { params: { category: '阀门' } })
  })
})

describe('configApi', () => {
  it('list calls GET /config', async () => {
    mockApi.get.mockResolvedValue({ data: [] })
    await configApi.list()
    expect(mockApi.get).toHaveBeenCalledWith('/config')
  })

  it('update calls PUT /config/:key', async () => {
    const payload = { value: { price_competitiveness: 0.5 } }
    mockApi.put.mockResolvedValue({ data: {} })
    await configApi.update('scoring_weights', payload)
    expect(mockApi.put).toHaveBeenCalledWith('/config/scoring_weights', payload)
  })
})
