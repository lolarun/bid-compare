<script setup lang="ts">
import { ref, reactive } from 'vue'
import { message } from 'ant-design-vue'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'
import dayjs from 'dayjs'

interface User {
  id: number
  username: string
  nickname: string
  role: '管理员' | '比价员' | '查看者'
  email: string
  phone: string
  status: '启用' | '停用'
  last_login: string
}

const data = ref<User[]>([
  { id: 1, username: 'admin', nickname: '杨科', role: '管理员', email: 'ke.yang@example.com', phone: '13800000001', status: '启用', last_login: '2026-05-19 09:12' },
  { id: 2, username: 'liu_pj', nickname: '刘佩珺', role: '比价员', email: 'liu.pj@example.com', phone: '13800000002', status: '启用', last_login: '2026-05-18 17:45' },
  { id: 3, username: 'wang_jb', nickname: '王建波', role: '比价员', email: 'wang.jb@example.com', phone: '13800000003', status: '启用', last_login: '2026-05-19 08:20' },
  { id: 4, username: 'zhang_my', nickname: '张明月', role: '查看者', email: 'zhang.my@example.com', phone: '13800000004', status: '启用', last_login: '2026-05-15 11:08' },
  { id: 5, username: 'chen_old', nickname: '陈志远', role: '比价员', email: 'chen@example.com', phone: '13800000005', status: '停用', last_login: '2025-12-10 16:32' },
])

const query = reactive({
  keyword: '',
  role: undefined as string | undefined,
})

const modalVisible = ref(false)
const editingId = ref<number | null>(null)
const form = reactive({
  username: '',
  nickname: '',
  role: '比价员' as User['role'],
  email: '',
  phone: '',
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

function openCreate() {
  editingId.value = null
  Object.assign(form, { username: '', nickname: '', role: '比价员', email: '', phone: '' })
  modalVisible.value = true
}

function openEdit(r: User) {
  editingId.value = r.id
  Object.assign(form, { username: r.username, nickname: r.nickname, role: r.role, email: r.email, phone: r.phone })
  modalVisible.value = true
}

function save() {
  if (editingId.value) {
    const target = data.value.find((u) => u.id === editingId.value)
    if (target) Object.assign(target, form)
    message.success('已更新')
  } else {
    data.value.push({
      id: Math.max(...data.value.map((u) => u.id), 0) + 1,
      ...form,
      status: '启用',
      last_login: dayjs().format('YYYY-MM-DD HH:mm'),
    })
    message.success('已新增')
  }
  modalVisible.value = false
}

function remove(id: number) {
  data.value = data.value.filter((u) => u.id !== id)
  message.success('已删除')
}

function toggleStatus(r: User) {
  r.status = r.status === '启用' ? '停用' : '启用'
  message.success(`已${r.status}`)
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
        <a-input v-model:value="query.keyword" placeholder="搜索用户名或昵称" style="width:200px" allow-clear />
        <a-select v-model:value="query.role" placeholder="全部角色" allow-clear style="width:140px">
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
        :pagination="{ pageSize: 10, showTotal: (t: number) => `共 ${t} 人` }"
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

    <a-modal v-model:open="modalVisible" :title="editingId ? '编辑用户' : '新增用户'" @ok="save" :width="520">
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
