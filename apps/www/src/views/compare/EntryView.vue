<!--
  design/44 §3 —— 比价项目列表：「招标比价分析」的落地页，回答"我要对哪个
  项目做比价"，跟数据管理下的「项目管理」（回答"维护项目主数据"）是两个
  问题，见 design/44 §3.1 的对照表。不重复项目管理的 create/edit/delete，
  只加"这个项目现在比价比到哪了"——各品类的当前轮次、已确认供应商数、
  定标基准轮、最近活动。

  D-1（用户 2026-08-27 决策，F3 收口）：新建项目按钮 F1 落地时对所有比价角色
  可见，明说了"P3 角色门禁上线后再收成仅管理员"——design/42 P3 落地
  （POST /api/projects 现在要求管理员），这里同步收口，不是新决定。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { PlusOutlined, ProjectOutlined, RightOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { projectApi } from '@/api'
import type { NextActionCode, ProjectsOverviewItem, QuoteRoundStatus } from '@/api/client'
import { useUserStore } from '@/stores/user'
import { ROLE_ADMIN } from '@/types/role'

const router = useRouter()
const userStore = useUserStore()
const isAdmin = computed(() => userStore.userInfo?.role === ROLE_ADMIN)

const items = ref<ProjectsOverviewItem[]>([])
const total = ref(0)
const loading = ref(false)
// design/45 §4.4：默认不显示空项目（无轮次+无报价+无清单会话）。开关不持久化
// ——它是"我这一下想看看"的临时动作，不是用户的长期偏好；持久化会让人下次
// 进来对着一屏空壳纳闷为什么。
const query = reactive({
  page: 1, page_size: 20,
  keyword: undefined as string | undefined,
  include_empty: false,
})

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

function roundStatusText(status: QuoteRoundStatus) {
  return status === 'open' ? '收集中' : '已关闭'
}
function roundStatusColor(status: QuoteRoundStatus) {
  return status === 'open' ? 'processing' : 'default'
}
function fmtDate(d: string | null) {
  if (!d) return '—'
  return d.replace('T', ' ').slice(0, 16)
}

// design/45 §4.3：标签文案由后端给（`next_action.label`），这里只决定颜色。
// 不在前端拼文案——两个入口（列表卡片、概述页）必须说同一句话。
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
function nextActionText(a: { label: string; count: number | null }) {
  return a.count == null ? a.label : `${a.label} ${a.count} 份`
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
        <h1 class="entry-view__title">招标比价分析</h1>
        <div class="entry-view__subtitle">
          {{ isAdmin ? '选择一个项目进入比价，或新建一个' : '选择一个项目进入比价' }}
        </div>
      </div>
      <a-space>
        <!-- design/45 §4.4：空项目默认不显示，开关不持久化 -->
        <a-checkbox
          v-model:checked="query.include_empty"
          @change="() => { query.page = 1; fetchData() }"
        >
          显示空项目
        </a-checkbox>
        <a-input-search
          v-model:value="query.keyword"
          placeholder="搜索项目名称 / 编号..."
          style="width:260px"
          @search="() => { query.page = 1; fetchData() }"
        />
        <!-- design/42 §8 D1 / design/44 F3：项目创建收口给管理员。 -->
        <a-button v-if="isAdmin" type="primary" @click="openCreateModal">
          <template #icon><PlusOutlined /></template>
          新建项目
        </a-button>
      </a-space>
    </div>

    <a-spin :spinning="loading">
      <a-empty v-if="!loading && items.length === 0"
               :description="isAdmin ? '还没有项目' : '还没有项目，请联系项目管理员创建'">
        <a-button v-if="isAdmin" type="primary" @click="openCreateModal">新建第一个项目</a-button>
      </a-empty>

      <div v-else class="entry-view__list">
        <a-card
          v-for="item in items" :key="item.project.id"
          class="entry-view__card" hoverable
          @click="openProject(item.project.id)"
        >
          <!--
            design/45 §4.2：右列是状态列，每品类一行（D-1）。轮次状态从左下角
            的小灰字移上来——原来视觉最重的右列只放了个时间戳，卡片因此回答不了
            「这个项目现在该干什么」。
          -->
          <div class="entry-view__card-row">
            <div class="entry-view__card-main">
              <div class="entry-view__card-name">
                <ProjectOutlined style="color:#1677ff" />
                {{ item.project.name }}
                <span v-if="item.project.code" class="entry-view__card-code">{{ item.project.code }}</span>
              </div>
              <div class="entry-view__card-time">最近活动 {{ fmtDate(lastActivityOf(item)) }}</div>
            </div>

            <div class="entry-view__card-status">
              <div v-if="item.categories.length === 0" class="entry-view__card-empty">
                首轮将在首次确认报价时自动开启
              </div>
              <div
                v-for="cat in item.categories" :key="cat.category"
                class="entry-view__cat"
              >
                <a-tag class="entry-view__cat-tag">{{ cat.category }}</a-tag>
                <span class="entry-view__cat-round">
                  <!-- 有清单但还没开轮时 current_round 为 null（design/45 §4.3） -->
                  <a-badge
                    v-if="cat.current_round"
                    :status="roundStatusColor(cat.current_round.status)"
                    :text="`第${cat.current_round.seq}轮 · ${roundStatusText(cat.current_round.status)}`"
                  />
                  <span v-else class="entry-view__cat-meta entry-view__cat-meta--muted">尚未开轮</span>
                </span>
                <span class="entry-view__cat-meta">{{ cat.submission_count }} 家已入库</span>
                <a-tag :color="nextActionColor(cat.next_action.code)">
                  {{ nextActionText(cat.next_action) }}
                </a-tag>
              </div>

              <!-- 待校对只有项目粒度：多品类时不硬分到某个品类，单独一行说清楚 -->
              <div
                v-if="item.pending_intake_count > 0 && item.categories.length !== 1"
                class="entry-view__cat"
              >
                <a-tag color="blue">待校对入库 {{ item.pending_intake_count }} 份</a-tag>
              </div>

              <a-button type="link" class="entry-view__enter">
                进入比价 <RightOutlined />
              </a-button>
            </div>
          </div>
        </a-card>
      </div>

      <a-pagination
        v-if="total > query.page_size"
        v-model:current="query.page"
        :page-size="query.page_size"
        :total="total"
        style="margin-top:16px; text-align:right"
        @change="fetchData"
      />
    </a-spin>

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

  &__list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  &__card {
    cursor: pointer;
  }

  &__card-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  &__card-main {
    flex: 1;
    min-width: 0;
  }

  &__card-name {
    font-size: 15px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__card-code {
    font-weight: 400;
    font-size: 12px;
    color: @text-color-secondary;
  }

  &__card-empty {
    font-size: 12px;
    color: @text-color-secondary;
  }

  // design/45 §4.2：右侧状态列。每品类一行，列对齐（品类名固定宽度）让多品类
  // 项目的轮次/家数/动作三列能竖着扫，而不是每行长短不一。
  &__card-status {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
    white-space: nowrap;
  }

  &__cat {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  &__cat-tag {
    min-width: 56px;
    text-align: center;
    margin-inline-end: 0;
  }

  &__cat-round {
    min-width: 108px;
    text-align: left;
  }

  &__cat-meta {
    font-size: 12px;
    color: @text-color-secondary;

    &--muted {
      opacity: 0.7;
    }
  }

  &__enter {
    padding-inline: 0;
    height: auto;
  }

  &__card-time {
    margin-top: 6px;
    font-size: 12px;
    color: @text-color-secondary;
  }
}
</style>
