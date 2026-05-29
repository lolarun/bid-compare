<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'
import { userApi } from '@/api'
import type { User } from '@/api/client'

const loading = ref(false)
const data = ref<User[]>([])
const total = ref(0)

const query = reactive({
  keyword: '',
  role: undefined as string | undefined,
  page: 1,
  page_size: 20,
})

const modalVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive({
  username: '',
  nickname: '',
  role: '比价员' as User['role'],
  email: '',
  phone: '',
  password: '',
})

const columns = [
  { title: '用户名', dataIndex: 'username', width: 130 },
  { title: '昵称', dataIndex: 'nickname', width: 100 },
  { title: '角色', dataIndex: 'role', width: 100 },
  { title: '邮箱', dataIndex: 'email', ellipsis: true },
  { title: '手机号', dataIndex: 'phone', width: 130 },
  { title: '状态', dataIndex: 'status', width: 90 },
  { title: '最近登录', dataIndex: 'last_login', width: 150 },
  { title: '操作', key: 'action', width: 150, fixed: 'right' as const },
]

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await userApi.list({
      keyword: query.keyword || undefined,
      role: query.role,
      page: query.page,
      page_size: query.page_size,
    })
    data.value = resp.items
    total.value = resp.total
  } catch {
    // interceptor handles notification
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)

function openCreate() {
  editingId.value = null
  Object.assign(form, { username: '', nickname: '', role: '比价员', email: '', phone: '', password: '' })
  modalVisible.value = true
}

function openEdit(r: User) {
  editingId.value = r.id
  Object.assign(form, { username: r.username, nickname: r.nickname, role: r.role, email: r.email, phone: r.phone, password: '' })
  modalVisible.value = true
}

async function save() {
  if (!form.username.trim()) {
    message.warning('请输入用户名')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const payload: Record<string, unknown> = {
        nickname: form.nickname,
        role: form.role,
        email: form.email,
        phone: form.phone,
      }
      if (form.password) payload.password = form.password
      await userApi.update(editingId.value, payload as Partial<User>)
      message.success('已更新')
    } else {
      if (!form.password) {
        message.warning('请输入密码')
        saving.value = false
        return
      }
      await userApi.create({ ...form } as never)
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
    await userApi.delete(id)
    message.success('已删除')
    fetchData()
  } catch {
    // interceptor handles notification
  }
}

async function toggleStatus(r: User) {
  try {
    await userApi.toggleStatus(r.id)
    message.success(r.status === '启用' ? '已停用' : '已启用')
    fetchData()
  } catch {
    // interceptor handles notification
  }
}

function roleColor(role: User['role']) {
  return role === '管理员' ? 'red' : role === '比价员' ? 'blue' : 'default'
}
</script>

<template>
  <div class="users-page">
    <div class="users-page__header">
      <div>
        <h1 class="users-page__title">用户管理</h1>
        <div class="users-page__subtitle">系统用户、角色与权限维护 · 一期仅管理员实操，比价员/查看者只读</div>
      </div>
      <a-button type="primary" @click="openCreate">
        <template #icon><PlusOutlined /></template>
        新增用户
      </a-button>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space>
        <a-input
          v-model:value="query.keyword"
          placeholder="搜索用户名或昵称"
          style="width:200px"
          allow-clear
          @press-enter="fetchData"
        />
        <a-select v-model:value="query.role" placeholder="全部角色" allow-clear style="width:140px" @change="fetchData">
          <a-select-option value="管理员">管理员</a-select-option>
          <a-select-option value="比价员">比价员</a-select-option>
          <a-select-option value="查看者">查看者</a-select-option>
        </a-select>
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
          showTotal: (t: number) => `共 ${t} 人`,
          onChange: (p: number) => { query.page = p; fetchData() },
        }"
        row-key="id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'username'">
            <span>
              <UserOutlined style="margin-right:4px;color:rgba(0,0,0,0.45)" />
              {{ (record as User).username }}
            </span>
          </template>
          <template v-else-if="column.dataIndex === 'role'">
            <a-tag :color="roleColor((record as User).role)">{{ (record as User).role }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'status'">
            <a-tag :color="(record as User).status === '启用' ? 'green' : 'default'">
              {{ (record as User).status }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a @click="openEdit(record as User)"><EditOutlined /> 编辑</a>
              <a @click="toggleStatus(record as User)">{{ (record as User).status === '启用' ? '停用' : '启用' }}</a>
              <a-popconfirm title="确认删除？" @confirm="remove((record as User).id)">
                <a style="color:#ff4d4f"><DeleteOutlined /> 删除</a>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal v-model:open="modalVisible" :title="editingId ? '编辑用户' : '新增用户'" @ok="save" :confirm-loading="saving" :width="520">
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="用户名" required>
              <a-input v-model:value="form.username" :disabled="!!editingId" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="昵称" required>
              <a-input v-model:value="form.nickname" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item :label="editingId ? '密码（留空不修改）' : '密码'" :required="!editingId">
          <a-input-password v-model:value="form.password" />
        </a-form-item>
        <a-form-item label="角色" required>
          <a-select v-model:value="form.role">
            <a-select-option value="管理员">管理员</a-select-option>
            <a-select-option value="比价员">比价员</a-select-option>
            <a-select-option value="查看者">查看者</a-select-option>
          </a-select>
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="邮箱">
              <a-input v-model:value="form.email" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="手机号">
              <a-input v-model:value="form.phone" />
            </a-form-item>
          </a-col>
        </a-row>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.users-page {
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
