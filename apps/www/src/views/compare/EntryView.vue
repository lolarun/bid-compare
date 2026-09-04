<!--
  design/44 §3 —— 比价项目列表：「招标比价」的落地页，回答"我要对哪个
  项目做比价"，跟数据管理下的「项目管理」（回答"维护项目主数据"）是两个
  问题，见 design/44 §3.1 的对照表。不重复项目管理的 create/edit/delete，
  只加"这个项目现在比价比到哪了"。

  D-1（用户 2026-08-27 决策，F3 收口）：新建项目按钮 F1 落地时对所有比价角色
  可见，明说了"P3 角色门禁上线后再收成仅管理员"——design/42 P3 落地
  （POST /api/projects 现在要求管理员），这里同步收口，不是新决定。

  2026-09-03（用户决策）：卡片列表改表格页，行粒度**一行一项目、只显示汇总**
  ——品类级明细（每个品类的轮次/入库家数）不再铺在列表里，进项目概述页看。
  代价是列表上看不到单个品类的轮次了；换来的是行高固定、翻页时行数可预期。
  筛选项仍只有 keyword + include_empty：GET /api/projects/overview 只支持这两个，
  多加筛选就得动后端。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { PlusOutlined, ProjectOutlined, RightOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { projectApi } from '@/api'
import type { NextActionCode, ProjectsOverviewItem } from '@/api/client'
import { useUserStore } from '@/stores/user'
import { ROLE_ADMIN } from '@/types/role'
import { typed, type AntPagination } from '@/utils/table'

const router = useRouter()
const userStore = useUserStore()
const isAdmin = computed(() => userStore.userInfo?.role === ROLE_ADMIN)

const items = ref<ProjectsOverviewItem[]>([])
const total = ref(0)
const loading = ref(false)
// 2026-09-03（用户决策，**撤回 design/45 §4.4 D-3**）：空项目默认**显示**。
// D-3 当初隐藏空项目的理由是"空项目都是自动生成的没用壳子"——那批壳子来自
// 「打开页面即建」，2026-08-21 已停掉。实际流程是：项目管理员先建好招标项目
// （此时就是个只有标题的空项目），项目部的人再上传报价做比价——空项目正是
// 项目部要找到才能开工的东西，隐藏它等于把入口列表在最需要的时刻清空。
const query = reactive({
  page: 1, page_size: 20,
  keyword: undefined as string | undefined,
  include_empty: true,
})

const columns = [
  { title: '项目名称', dataIndex: 'name', width: 260 },
  { title: '项目编号', dataIndex: 'code', width: 150 },
  { title: '品类', dataIndex: 'categories', width: 110 },
  { title: '待办', dataIndex: 'nextAction', width: 260 },
  { title: '待校对入库', dataIndex: 'pending', width: 120 },
  { title: '最近活动', dataIndex: 'lastActivity', width: 150 },
  { title: '操作', key: 'action', width: 120, fixed: 'right' as const },
]

async function fetchData() {
  loading.value = true
  try {
    const { data } = await projectApi.overview(query as Record<string, unknown>)
    items.value = data.items
    total.value = data.total
  } catch {
    items.value = []
    total.value = 0
    message.error('比价项目列表加载失败，请重试')
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)

function handleSearch() {
  query.page = 1
  fetchData()
}

function handleReset() {
  query.page = 1
  query.keyword = undefined
  query.include_empty = true
  fetchData()
}

function openProject(projectId: number) {
  router.push(`/workspace/${projectId}`)
}

// ── 新建项目（D-1）：复用项目管理的最小字段集，创建后直接进工作台 ──────────
const createModalOpen = ref(false)
const creating = ref(false)
const createForm = reactive({ name: '', code: '' })

function openCreateModal() {
  createForm.name = ''
  createForm.code = ''
  createModalOpen.value = true
}

async function handleCreate() {
  if (!createForm.name.trim()) {
    message.warning('请输入项目名称')
    return
  }
  creating.value = true
  try {
    const { data } = await projectApi.create({
      name: createForm.name, code: createForm.code, location: '', status: '进行中', remark: '',
    })
    createModalOpen.value = false
    // 刚建的项目必然是空的，概述页无话可说——直接送进工作台去传第一份文件。
    // 已有项目走 openProject()，那条路径才落在概述页。
    router.push(`/workspace/${data.id}/compare`)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '创建失败')
  } finally {
    creating.value = false
  }
}

function fmtDate(d: string | null) {
  if (!d) return '—'
  return d.replace('T', ' ').slice(0, 16)
}

// design/45 §4.3：标签文案由后端给（`next_action.label`），这里只决定颜色。
// 不在前端拼文案——两个入口（列表、概述页）必须说同一句话。
const NEXT_ACTION_COLOR: Record<NextActionCode, string> = {
  pending_upload: 'default',
  list_unconfirmed: 'orange',
  pending_intake: 'blue',
  ready_to_compare: 'green',
  basis_set: 'gold',
}
function nextActionColor(code: NextActionCode) {
  return NEXT_ACTION_COLOR[code] ?? 'default'
}

/** 一行一项目要把 N 个品类的待办压成一列：按 code 去重，标签文案仍旧全部来自
 *  后端。**多个品类共用一个 code 时只标品类数，不合并 count**——count 是后端
 *  按品类裁定的份数，前端加总就等于自造了一个后端没给过的数字
 *  （CLAUDE.md §4：next_action 由 derive_next_action 唯一裁定）。 */
function nextActionSummary(item: ProjectsOverviewItem) {
  const byCode = new Map<NextActionCode, { label: string; count: number | null; cats: number }>()
  for (const cat of item.categories) {
    const seen = byCode.get(cat.next_action.code)
    if (seen) seen.cats += 1
    else byCode.set(cat.next_action.code, { label: cat.next_action.label, count: cat.next_action.count, cats: 1 })
  }
  return [...byCode.entries()].map(([code, v]) => ({
    code,
    text: v.cats > 1
      ? `${v.label} · ${v.cats} 个品类`
      : v.count == null ? v.label : `${v.label} ${v.count} 份`,
  }))
}

/** 最近活动取各品类里最新的一个，而不是 categories[0]——分类是按名字排序的，
 *  [0] 只是"阀门排在电缆前面"，跟时间没关系。 */
function lastActivityOf(item: ProjectsOverviewItem): string | null {
  const ds = item.categories.map(c => c.last_activity).filter((d): d is string => !!d)
  return ds.length ? ds.sort().slice(-1)[0] : null
}
</script>

<template>
  <div class="entry-view">
    <div class="entry-view__header">
      <div>
        <h1 class="entry-view__title">招标比价</h1>
        <div class="entry-view__subtitle">
          {{ isAdmin ? '选择一个项目进入比价，或新建一个' : '选择一个项目进入比价' }}
        </div>
      </div>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space :wrap="true">
        <a-input
          v-model:value="query.keyword"
          placeholder="搜索项目名称 / 编号..."
          style="width:280px"
          allow-clear
          @press-enter="handleSearch"
        />
        <!-- 2026-09-03：默认勾选（撤回 design/45 §4.4 D-3），取消勾选可只看已开工的项目 -->
        <a-checkbox v-model:checked="query.include_empty">显示空项目</a-checkbox>
        <a-button type="primary" @click="handleSearch">搜 索</a-button>
        <a-button @click="handleReset">重 置</a-button>
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <div class="entry-view__toolbar">
        <span class="entry-view__toolbar-title">项目列表</span>
        <!-- design/42 §8 D1 / design/44 F3：项目创建收口给管理员。 -->
        <a-button v-if="isAdmin" type="primary" @click="openCreateModal">
          <template #icon><PlusOutlined /></template>
          新建项目
        </a-button>
      </div>

      <a-table
        :columns="columns"
        :data-source="items"
        :loading="loading"
        :pagination="{
          current: query.page,
          pageSize: query.page_size,
          total,
          showSizeChanger: true,
          showTotal: (t: number) => `共 ${t} 个项目`,
        }"
        :scroll="{ x: 1170 }"
        :row-key="(record: ProjectsOverviewItem) => record.project.id"
        size="middle"
        @change="(pag: AntPagination) => { query.page = pag.current; query.page_size = pag.pageSize; fetchData() }"
      >
        <template #emptyText>
          <!-- 取消勾选「显示空项目」时筛空 ≠ 没有项目。原来两种情形都说"还没有
               项目"，而库里明明有——空状态不能说谎。 -->
          <a-empty v-if="!query.include_empty" description="没有已开工的项目——还没有报价或采购清单的项目已被隐藏">
            <a-button @click="() => { query.include_empty = true; handleSearch() }">显示空项目</a-button>
          </a-empty>
          <a-empty v-else-if="query.keyword" :description="`没有匹配「${query.keyword}」的项目`">
            <a-button @click="handleReset">清空筛选</a-button>
          </a-empty>
          <a-empty v-else :description="isAdmin ? '还没有项目' : '还没有项目，请联系项目管理员创建'">
            <a-button v-if="isAdmin" type="primary" @click="openCreateModal">新建第一个项目</a-button>
          </a-empty>
        </template>

        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'name'">
            <a class="entry-view__name" @click="openProject(typed<ProjectsOverviewItem>(record).project.id)">
              <ProjectOutlined style="margin-right:6px;color:#1677ff" />
              {{ typed<ProjectsOverviewItem>(record).project.name }}
            </a>
          </template>

          <template v-else-if="column.dataIndex === 'code'">
            <span v-if="typed<ProjectsOverviewItem>(record).project.code">
              {{ typed<ProjectsOverviewItem>(record).project.code }}
            </span>
            <span v-else class="entry-view__muted">—</span>
          </template>

          <template v-else-if="column.dataIndex === 'categories'">
            <span v-if="typed<ProjectsOverviewItem>(record).categories.length">
              {{ typed<ProjectsOverviewItem>(record).categories.length }} 个
            </span>
            <span v-else class="entry-view__muted">—</span>
          </template>

          <template v-else-if="column.dataIndex === 'nextAction'">
            <a-space v-if="typed<ProjectsOverviewItem>(record).categories.length" :size="4" :wrap="true">
              <a-tag
                v-for="a in nextActionSummary(typed<ProjectsOverviewItem>(record))"
                :key="a.code"
                :color="nextActionColor(a.code)"
              >
                {{ a.text }}
              </a-tag>
            </a-space>
            <span v-else class="entry-view__muted">首轮将在首次确认报价时自动开启</span>
          </template>

          <template v-else-if="column.dataIndex === 'pending'">
            <!-- 待校对只有项目粒度：job 上没有可靠品类，硬分到品类会让数字跳动 -->
            <a-tag v-if="typed<ProjectsOverviewItem>(record).pending_intake_count > 0" color="blue">
              {{ typed<ProjectsOverviewItem>(record).pending_intake_count }} 份
            </a-tag>
            <span v-else class="entry-view__muted">—</span>
          </template>

          <template v-else-if="column.dataIndex === 'lastActivity'">
            {{ fmtDate(lastActivityOf(typed<ProjectsOverviewItem>(record))) }}
          </template>

          <template v-else-if="column.key === 'action'">
            <a @click="openProject(typed<ProjectsOverviewItem>(record).project.id)">
              进入比价 <RightOutlined />
            </a>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      v-model:open="createModalOpen"
      title="新建项目"
      :confirm-loading="creating"
      ok-text="创建并进入"
      cancel-text="取消"
      @ok="handleCreate"
    >
      <a-form :label-col="{ span: 5 }" :wrapper-col="{ span: 18 }" style="margin-top:16px">
        <a-form-item label="项目名称" required>
          <a-input v-model:value="createForm.name" placeholder="请输入项目名称" />
        </a-form-item>
        <a-form-item label="项目编号">
          <a-input v-model:value="createForm.code" placeholder="可选，如 P2026-001" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.entry-view {
  padding: 16px 24px;

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

  &__toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 0 12px;
  }

  &__toolbar-title {
    font-size: 15px;
    font-weight: 600;
    color: @heading-color;
  }

  &__name {
    font-weight: 500;
  }

  &__muted {
    color: @text-color-secondary;
  }
}
</style>
