<script setup lang="ts">
import { ref, reactive, onMounted, watch } from 'vue'
import { PlusOutlined, EditOutlined, DeleteOutlined, ProjectOutlined } from '@ant-design/icons-vue'
import { message, Modal } from 'ant-design-vue'
import { projectApi } from '@/api'
import type { Project } from '@/api/client'

const data = ref<Project[]>([])
const total = ref(0)
const loading = ref(false)

const query = reactive({
  page: 1,
  page_size: 20,
  keyword: undefined as string | undefined,
  status: undefined as string | undefined,
})

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await projectApi.list(query as Record<string, unknown>)
    data.value = resp.items
    total.value = resp.total
  } catch {
    data.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

const columns = [
  { title: '项目名称', dataIndex: 'name', width: 220, ellipsis: true },
  { title: '项目编号', dataIndex: 'code', width: 140, ellipsis: true },
  { title: '项目地点', dataIndex: 'location', width: 160, ellipsis: true },
  { title: '状态', dataIndex: 'status', width: 100, align: 'center' as const },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
  { title: '创建时间', dataIndex: 'created_at', width: 170 },
  { title: '操作', key: 'action', width: 120, fixed: 'right' as const },
]

const modalVisible = ref(false)
const modalTitle = ref('新建项目')
const editingId = ref<number | null>(null)
const saving = ref(false)

const form = reactive({
  name: '',
  code: '',
  location: '',
  status: '进行中',
  remark: '',
})

function resetForm() {
  form.name = ''
  form.code = ''
  form.location = ''
  form.status = '进行中'
  form.remark = ''
  editingId.value = null
}

function openCreate() {
  resetForm()
  modalTitle.value = '新建项目'
  modalVisible.value = true
}

function openEdit(record: Project) {
  editingId.value = record.id
  form.name = record.name
  form.code = record.code
  form.location = record.location
  form.status = record.status
  form.remark = record.remark
  modalTitle.value = '编辑项目'
  modalVisible.value = true
}

async function handleSave() {
  if (!form.name.trim()) {
    message.warning('请输入项目名称')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      await projectApi.update(editingId.value, { ...form })
      message.success('项目已更新')
    } else {
      await projectApi.create({ ...form })
      message.success('项目已创建')
    }
    modalVisible.value = false
    fetchData()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '操作失败')
  } finally {
    saving.value = false
  }
}

function handleDelete(record: Project) {
  Modal.confirm({
    title: '确认删除',
    content: `确定要删除项目「${record.name}」吗？删除后不可恢复。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await projectApi.delete(record.id)
        message.success('已删除')
        fetchData()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '删除失败')
      }
    },
  })
}

function statusColor(s: string) {
  if (s === '进行中') return 'processing'
  if (s === '已完成') return 'success'
  if (s === '已暂停') return 'warning'
  return 'default'
}

function fmtDate(d: string | null) {
  if (!d) return '—'
  return d.replace('T', ' ').slice(0, 16)
}

watch(
  () => [query.status],
  () => { query.page = 1; fetchData() },
)

onMounted(fetchData)
</script>

<template>
  <div class="projects-page">
    <div class="projects-page__header">
      <div>
        <h1 class="projects-page__title">项目管理</h1>
        <div class="projects-page__subtitle">管理所有项目信息，支持修改名称、补充信息和删除</div>
      </div>
      <div class="flex gap-8">
        <a-button type="primary" @click="openCreate">
          <template #icon><PlusOutlined /></template>
          新建项目
        </a-button>
      </div>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space :wrap="true">
        <a-select v-model:value="query.status" placeholder="全部状态" allow-clear style="width:130px">
          <a-select-option value="进行中">进行中</a-select-option>
          <a-select-option value="已完成">已完成</a-select-option>
          <a-select-option value="已暂停">已暂停</a-select-option>
        </a-select>
        <a-input-search
          v-model:value="query.keyword"
          placeholder="搜索项目名称 / 编号..."
          style="width:280px"
          @search="() => { query.page = 1; fetchData() }"
        />
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :loading="loading"
        :pagination="{
          current: query.page,
          pageSize: query.page_size,
          total,
          showSizeChanger: true,
          showTotal: (t: number) => `共 ${t} 个项目`,
        }"
        :scroll="{ x: 1100 }"
        row-key="id"
        size="middle"
        @change="(pag: any) => { query.page = pag.current; query.page_size = pag.pageSize; fetchData() }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'name'">
            <ProjectOutlined style="margin-right:6px;color:#1677ff" />
            <span style="font-weight:500">{{ (record as Project).name }}</span>
          </template>
          <template v-else-if="column.dataIndex === 'status'">
            <a-badge :status="statusColor((record as Project).status)" :text="(record as Project).status" />
          </template>
          <template v-else-if="column.dataIndex === 'created_at'">
            {{ fmtDate((record as Project).created_at) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a @click="openEdit(record as Project)"><EditOutlined /> 编辑</a>
              <a style="color:#ff4d4f" @click="handleDelete(record as Project)"><DeleteOutlined /></a>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      v-model:open="modalVisible"
      :title="modalTitle"
      :confirm-loading="saving"
      ok-text="保存"
      cancel-text="取消"
      @ok="handleSave"
    >
      <a-form :label-col="{ span: 5 }" :wrapper-col="{ span: 18 }" style="margin-top:16px">
        <a-form-item label="项目名称" required>
          <a-input v-model:value="form.name" placeholder="请输入项目名称" />
        </a-form-item>
        <a-form-item label="项目编号">
          <a-input v-model:value="form.code" placeholder="可选，如 P2026-001" />
        </a-form-item>
        <a-form-item label="项目地点">
          <a-input v-model:value="form.location" placeholder="可选" />
        </a-form-item>
        <a-form-item label="状态">
          <a-select v-model:value="form.status">
            <a-select-option value="进行中">进行中</a-select-option>
            <a-select-option value="已完成">已完成</a-select-option>
            <a-select-option value="已暂停">已暂停</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="form.remark" :rows="3" placeholder="可选" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.projects-page {
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
