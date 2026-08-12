<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { brandTierApi } from '@/api'
import type { BrandTier } from '@/api/client'

// R4-3：brandTierApi 一直是零调用方——后端 CRUD 早就实现（apps/api/routes/
// brand_tiers.py），品牌档位（国产/合资/三档）只能靠脚本或直接改库维护，没有
// 界面入口。这里补一个最小可用的维护页，风格照抄 system/UsersView.vue。

const loading = ref(false)
const data = ref<BrandTier[]>([])
const categoryFilter = ref<string>('')

const columns = [
  { title: '品牌名称', dataIndex: 'brand_name', width: 200 },
  { title: '档位', dataIndex: 'tier', width: 120 },
  { title: '适用品类', dataIndex: 'category', width: 160 },
  { title: '操作', key: 'action', width: 140, fixed: 'right' as const },
]

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await brandTierApi.list(
      categoryFilter.value ? { category: categoryFilter.value } : undefined,
    )
    data.value = resp
  } catch {
    // interceptor handles notification
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)

const modalVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive({
  brand_name: '',
  tier: '国产' as BrandTier['tier'],
  category: '',
})

function openCreate() {
  editingId.value = null
  Object.assign(form, { brand_name: '', tier: '国产', category: '' })
  modalVisible.value = true
}

function openEdit(r: BrandTier) {
  editingId.value = r.id
  Object.assign(form, { brand_name: r.brand_name, tier: r.tier, category: r.category ?? '' })
  modalVisible.value = true
}

async function save() {
  if (!form.brand_name.trim()) {
    message.warning('请输入品牌名称')
    return
  }
  saving.value = true
  try {
    const category = form.category.trim() || null
    if (editingId.value) {
      await brandTierApi.update(editingId.value, { tier: form.tier, category })
      message.success('已更新')
    } else {
      // POST 本身是 upsert（同 brand_name+category 命中则更新档位），
      // 见 brand_tiers.py create_brand_tier
      await brandTierApi.create({ brand_name: form.brand_name.trim(), tier: form.tier, category })
      message.success('已新增')
    }
    modalVisible.value = false
    fetchData()
  } catch {
    // interceptor handles notification
  } finally {
    saving.value = false
  }
}

async function remove(id: number) {
  try {
    await brandTierApi.delete(id)
    message.success('已删除')
    fetchData()
  } catch {
    // interceptor handles notification
  }
}

function tierColor(tier: BrandTier['tier']) {
  return tier === '合资' ? 'purple' : tier === '国产' ? 'blue' : 'default'
}
</script>

<template>
  <div class="brand-tiers-page">
    <div class="brand-tiers-page__header">
      <div>
        <h1 class="brand-tiers-page__title">品牌档位维护</h1>
        <div class="brand-tiers-page__subtitle">
          国产 / 合资 / 三档标签 · 供品牌推荐（邀标建议）与评审展示引用
        </div>
      </div>
      <a-button type="primary" @click="openCreate">
        <template #icon><PlusOutlined /></template>
        新增品牌档位
      </a-button>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space>
        <a-input
          v-model:value="categoryFilter"
          placeholder="按品类过滤（留空为全部）"
          style="width:220px"
          allow-clear
          @press-enter="fetchData"
          @clear="fetchData"
        />
        <a-button @click="fetchData">查询</a-button>
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table :columns="columns" :data-source="data" :loading="loading" :pagination="false" row-key="id" size="middle">
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'tier'">
            <a-tag :color="tierColor((record as BrandTier).tier)">{{ (record as BrandTier).tier }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'category'">
            {{ (record as BrandTier).category || '（全部品类通用）' }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a @click="openEdit(record as BrandTier)"><EditOutlined /> 编辑</a>
              <a-popconfirm title="确认删除该品牌档位？" @confirm="remove((record as BrandTier).id)">
                <a style="color:#ff4d4f"><DeleteOutlined /> 删除</a>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      v-model:open="modalVisible"
      :title="editingId ? '编辑品牌档位' : '新增品牌档位'"
      @ok="save"
      :confirm-loading="saving"
      :width="480"
    >
      <a-form layout="vertical">
        <a-form-item label="品牌名称" required>
          <a-input v-model:value="form.brand_name" :disabled="!!editingId" />
        </a-form-item>
        <a-form-item label="档位" required>
          <a-select v-model:value="form.tier">
            <a-select-option value="国产">国产</a-select-option>
            <a-select-option value="合资">合资</a-select-option>
            <a-select-option value="三档">三档</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="适用品类（留空表示全部品类通用）">
          <a-input v-model:value="form.category" placeholder="如：阀门 / 管件" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.brand-tiers-page {
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
}
</style>
