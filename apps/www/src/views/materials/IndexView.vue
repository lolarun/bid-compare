<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ExportOutlined, SwapOutlined, FolderOutlined } from '@ant-design/icons-vue'
import { materialApi, exportApi } from '@/api'
import type { Material, StandardizeResult, ExtendedAttrSchema } from '@/api/client'
import { doExport } from '@/utils/download'
import type { DataNode } from 'ant-design-vue/es/tree'

const data = ref<Material[]>([])
const total = ref(0)
const loading = ref(false)
const modalVisible = ref(false)
const editingId = ref<number | null>(null)

const query = reactive({
  page: 1,
  page_size: 20,
  profession: undefined as string | undefined,
  category: undefined as string | undefined,
  sub_category: undefined as string | undefined,
  keyword: undefined as string | undefined,
})

const form = reactive({
  standard_name: '',
  profession: '',
  category: '',
  sub_category: '',
  spec: '',
  material_type: '',
  unit: '',
  brand: '',
})

const categories = ref<{ profession: string; category: string; count: number }[]>([])
const selectedTreeKeys = ref<string[]>([])
const expandedKeys = ref<string[]>([])

const PROFESSIONS = ['电气', '给排水', '暖通']
const CAT_OPTIONS: Record<string, string[]> = {
  '电气': ['桥架', '母线槽', '配电箱'],
  '给排水': ['阀门', '不锈钢管', '水箱', '潜水泵'],
  '暖通': ['风口风阀', '风机盘管', '空调泵'],
}

// 树状结构：专业 → 品类 → 子类
const treeData = computed<DataNode[]>(() => {
  const grouped = new Map<string, { category: string; count: number }[]>()
  for (const c of categories.value) {
    if (!grouped.has(c.profession)) grouped.set(c.profession, [])
    grouped.get(c.profession)!.push({ category: c.category, count: c.count })
  }
  const allRoot: DataNode = {
    key: '__all__',
    title: `全部物料 (${total.value || categories.value.reduce((s, x) => s + x.count, 0)})`,
    icon: () => h(FolderOutlined),
  }
  const profNodes: DataNode[] = PROFESSIONS.map((p) => {
    const cats = grouped.get(p) ?? CAT_OPTIONS[p].map((c) => ({ category: c, count: 0 }))
    const total = cats.reduce((s, c) => s + c.count, 0)
    return {
      key: `p-${p}`,
      title: `${p} (${total})`,
      icon: () => h(FolderOutlined),
      children: cats.map((c) => ({
        key: `c-${p}-${c.category}`,
        title: `${c.category} (${c.count})`,
        isLeaf: true,
      })),
    }
  })
  return [allRoot, ...profNodes]
})

function onTreeSelect(keys: (string | number)[]) {
  selectedTreeKeys.value = keys as string[]
  const k = keys[0] as string | undefined
  if (!k || k === '__all__') {
    query.profession = undefined
    query.category = undefined
  } else if (k.startsWith('p-')) {
    query.profession = k.slice(2)
    query.category = undefined
  } else if (k.startsWith('c-')) {
    const rest = k.slice(2)
    const idx = rest.indexOf('-')
    query.profession = rest.slice(0, idx)
    query.category = rest.slice(idx + 1)
  }
  query.page = 1
  fetchData()
}

// ─── 表格 ───────────────────────────────────────────────────────────────
const columns = [
  { title: '物料编码', dataIndex: 'material_code', width: 110, customRender: ({ text }: { text: string }) => text || '—' },
  { title: '名称', dataIndex: 'standard_name', ellipsis: true },
  { title: '专业', dataIndex: 'profession', width: 70 },
  { title: '品类', dataIndex: 'category', width: 80 },
  { title: '子类', dataIndex: 'sub_category', width: 110 },
  { title: '规格', dataIndex: 'spec', width: 130, ellipsis: true },
  { title: '材质', dataIndex: 'material_type', width: 110, ellipsis: true },
  { title: '推荐品牌', dataIndex: 'brand', width: 120, ellipsis: true },
  {
    title: '参考价格区间',
    key: 'price_range',
    width: 160,
    customRender: ({ record }: { record: Material }) => {
      const lo = record.ref_price_low
      const hi = record.ref_price_high
      if (lo === null && hi === null) return '—'
      return `¥${lo?.toFixed(0) ?? '—'} ~ ¥${hi?.toFixed(0) ?? '—'}`
    },
  },
  { title: '操作', key: 'action', width: 140, fixed: 'right' as const },
]

// ─── CRUD ───────────────────────────────────────────────────────────────
async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await materialApi.list(query as Record<string, unknown>)
    data.value = resp.items
    total.value = resp.total
  } catch {
    data.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function fetchCategories() {
  try {
    const { data: cats } = await materialApi.categories()
    categories.value = cats
    expandedKeys.value = PROFESSIONS.map((p) => `p-${p}`)
  } catch {
    categories.value = []
  }
}

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    standard_name: '', profession: '', category: '', sub_category: '',
    spec: '', material_type: '', unit: '', brand: '',
  })
  modalVisible.value = true
}

function openEdit(record: Material) {
  editingId.value = record.id
  Object.assign(form, {
    standard_name: record.standard_name,
    profession: record.profession,
    category: record.category,
    sub_category: record.sub_category,
    spec: record.spec,
    material_type: record.material_type,
    unit: record.unit,
    brand: record.brand,
  })
  modalVisible.value = true
}

async function handleSave() {
  try {
    if (editingId.value) {
      await materialApi.update(editingId.value, form)
      message.success('更新成功')
    } else {
      await materialApi.create(form)
      message.success('创建成功')
    }
    modalVisible.value = false
    fetchData()
    fetchCategories()
  } catch {
    message.error('操作失败')
  }
}

async function handleDelete(id: number) {
  try {
    await materialApi.delete(id)
    message.success('已删除')
    fetchData()
  } catch {
    message.error('删除失败')
  }
}

// ─── 名称标准化弹窗 ─────────────────────────────────────────────────────
const stdVisible = ref(false)
const stdInput = ref('')
const stdCategory = ref<string | undefined>(undefined)
const stdResult = ref<StandardizeResult | null>(null)
const stdLoading = ref(false)

async function doStandardize() {
  if (!stdInput.value.trim()) {
    message.warning('请输入物料名称/规格')
    return
  }
  stdLoading.value = true
  try {
    const { data: res } = await materialApi.standardize({
      text: stdInput.value,
      category: stdCategory.value,
    })
    stdResult.value = res
  } catch {
    message.error('标准化失败')
  } finally {
    stdLoading.value = false
  }
}

// ─── 扩展属性弹窗 ───────────────────────────────────────────────────────
const schemaVisible = ref(false)
const schemaData = ref<ExtendedAttrSchema | null>(null)
const schemaLoading = ref(false)

async function showSchema(category: string) {
  schemaLoading.value = true
  schemaVisible.value = true
  try {
    const { data: res } = await materialApi.extendedSchema(category)
    schemaData.value = res
  } catch {
    schemaData.value = null
  } finally {
    schemaLoading.value = false
  }
}

onMounted(() => {
  fetchData()
  fetchCategories()
})
</script>

<template>
  <div class="materials-page">
    <!-- 标题区 -->
    <div class="materials-page__header">
      <div>
        <h1 class="materials-page__title">物料主数据</h1>
        <div class="materials-page__subtitle">物料标准库 · 名称、规格、材质统一定义 · 支撑比价的语义基础</div>
      </div>
      <div class="flex gap-8">
        <a-button @click="stdVisible = true">
          <template #icon><SwapOutlined /></template>
          名称标准化
        </a-button>
        <a-button @click="doExport(() => exportApi.materials({ category: query.category }), 'MEMPAS_物料主数据.xlsx')">
          <template #icon><ExportOutlined /></template>
          导出
        </a-button>
        <a-button type="primary" @click="openCreate">
          <template #icon><PlusOutlined /></template>
          新增标准
        </a-button>
      </div>
    </div>

    <!-- 主体：左树 + 右表 -->
    <div class="materials-page__body">
      <aside class="materials-page__tree">
        <div class="materials-page__tree-title">专业分类</div>
        <a-tree
          :tree-data="treeData"
          :selected-keys="selectedTreeKeys"
          :expanded-keys="expandedKeys"
          :show-icon="true"
          block-node
          @select="onTreeSelect"
          @update:expanded-keys="(k: (string | number)[]) => expandedKeys = k as string[]"
        />
      </aside>

      <section class="materials-page__main">
        <div class="materials-page__toolbar">
          <span class="materials-page__crumb">
            {{ query.profession ? `${query.profession}` : '全部' }}
            <template v-if="query.category"> / {{ query.category }}</template>
          </span>
          <span style="color:rgba(0,0,0,0.45);font-size:12px;margin-left:8px">{{ total }} 项标准条目</span>
          <a-input-search
            v-model:value="query.keyword"
            placeholder="搜索条目"
            style="width:240px;margin-left:auto"
            @search="() => { query.page = 1; fetchData() }"
          />
        </div>

        <a-table
          :columns="columns"
          :data-source="data"
          :loading="loading"
          :pagination="{
            current: query.page,
            pageSize: query.page_size,
            total,
            showSizeChanger: true,
            showTotal: (t: number) => `共 ${t} 条`,
          }"
          :scroll="{ x: 1200 }"
          row-key="id"
          size="middle"
          @change="(pag: any) => { query.page = pag.current; query.page_size = pag.pageSize; fetchData() }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'action'">
              <a-space>
                <a @click="openEdit(record as Material)">编辑</a>
                <a @click="showSchema((record as Material).category)">属性</a>
                <a-popconfirm title="确认删除？" @confirm="handleDelete((record as Material).id)">
                  <a style="color:#ff4d4f">停用</a>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </section>
    </div>

    <!-- 新增/编辑物料 -->
    <a-modal
      v-model:open="modalVisible"
      :title="editingId ? '编辑标准条目' : '新增标准条目'"
      @ok="handleSave"
      :width="640"
    >
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="物料名称" required>
              <a-input v-model:value="form.standard_name" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="规格型号">
              <a-input v-model:value="form.spec" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item label="专业" required>
              <a-select v-model:value="form.profession">
                <a-select-option v-for="p in PROFESSIONS" :key="p" :value="p">{{ p }}</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="品类" required>
              <a-select v-model:value="form.category">
                <a-select-option
                  v-for="c in (form.profession ? CAT_OPTIONS[form.profession] || [] : Object.values(CAT_OPTIONS).flat())"
                  :key="c"
                  :value="c"
                >{{ c }}</a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="子类">
              <a-input v-model:value="form.sub_category" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item label="材质">
              <a-input v-model:value="form.material_type" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="单位">
              <a-input v-model:value="form.unit" placeholder="m/台/套" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="推荐品牌">
              <a-input v-model:value="form.brand" />
            </a-form-item>
          </a-col>
        </a-row>
      </a-form>
    </a-modal>

    <!-- 标准化弹窗 -->
    <a-modal v-model:open="stdVisible" title="物料名称标准化" :footer="null" :width="600">
      <a-form layout="vertical">
        <a-form-item label="物料名称/规格">
          <a-input v-model:value="stdInput" placeholder="例：热镀锌桥架 300*150 Φ108" />
        </a-form-item>
        <a-form-item label="品类（可选）">
          <a-select v-model:value="stdCategory" allow-clear placeholder="选择品类提升准确度" style="width:100%">
            <a-select-option v-for="c in Object.values(CAT_OPTIONS).flat()" :key="c" :value="c">{{ c }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" :loading="stdLoading" @click="doStandardize">标准化</a-button>
        </a-form-item>
      </a-form>
      <div v-if="stdResult" style="margin-top: 16px">
        <a-descriptions :column="1" bordered size="small">
          <a-descriptions-item label="原始输入">{{ stdResult.original }}</a-descriptions-item>
          <a-descriptions-item label="标准化结果">
            <span style="font-weight: bold; color: #1677ff">{{ stdResult.standardized }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="变更项">
            <a-tag v-for="(c, i) in stdResult.changes" :key="i" color="blue" style="margin-bottom: 4px">{{ c }}</a-tag>
            <span v-if="stdResult.changes.length === 0" style="color: #999">无需变更</span>
          </a-descriptions-item>
        </a-descriptions>
      </div>
    </a-modal>

    <!-- 扩展属性 -->
    <a-modal
      v-model:open="schemaVisible"
      :title="`${schemaData?.category || ''} 扩展属性定义`"
      :footer="null"
      :width="600"
    >
      <a-spin :spinning="schemaLoading">
        <a-table v-if="schemaData" :data-source="schemaData.fields" :pagination="false" row-key="key" size="small">
          <a-table-column title="属性" data-index="label" />
          <a-table-column title="字段名" data-index="key" />
          <a-table-column title="数据来源" data-index="source" />
          <a-table-column title="比价角色" data-index="role">
            <template #default="{ record }">
              <a-tag :color="record.role === '匹配' ? 'blue' : 'orange'">{{ record.role }}</a-tag>
            </template>
          </a-table-column>
        </a-table>
      </a-spin>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.materials-page {
  &__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 16px;
  }

  &__title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: @heading-color;
  }

  &__subtitle {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }

  &__body {
    display: flex;
    gap: 16px;
    align-items: flex-start;
  }

  &__tree {
    width: 240px;
    flex-shrink: 0;
    background: #fff;
    border-radius: @border-radius-lg;
    padding: 16px 12px;
    box-shadow: @shadow-1;
    max-height: calc(100vh - 200px);
    overflow: auto;
  }

  &__tree-title {
    font-size: 13px;
    color: @text-color-secondary;
    font-weight: 600;
    margin-bottom: 8px;
    padding: 0 4px;
  }

  &__main {
    flex: 1;
    min-width: 0;
    background: #fff;
    border-radius: @border-radius-lg;
    padding: 16px 20px;
    box-shadow: @shadow-1;
  }

  &__toolbar {
    display: flex;
    align-items: center;
    margin-bottom: 12px;
  }

  &__crumb {
    font-size: 15px;
    font-weight: 600;
    color: @heading-color;
  }
}
</style>
