<!--
  design/27 §10 步骤4 —— 对齐核查独立视图（user decision D1）。从工作台头部
  进入，"返回工作台"随时切回，双向自由导航、切换不丢状态（工作台是独立
  路由，AnchorReviewMatrix 自己管自己的数据加载，两边状态天然不互相影响）。

  D2 条件条款验收（本文件是"清单只读"这个决定成立的前提，不是可选项）：
  每一行必须有不依赖改清单的终局动作——AnchorReviewMatrix.vue 已有的三种
  （align 到主匹配/候选锚点、exclude 排除、missing-ack 缺报确认）+ 本文件
  新增的"清单有误？重新上传"逃生口，四者合起来覆盖 D2 要求的全部终局路径。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LeftOutlined, FileSearchOutlined } from '@ant-design/icons-vue'
import AnchorReviewMatrix from './components/AnchorReviewMatrix.vue'

const route = useRoute()
const router = useRouter()

const projectId = computed(() => Number(route.params.projectId))
const category = computed(() => String(route.query.category || ''))
const submissionIds = computed(() => {
  const raw = route.query.submission_ids
  if (!raw) return undefined
  return String(raw).split(',').map(Number).filter((n) => !Number.isNaN(n))
})

function backToWorkspace() {
  router.push(`/workspace/${projectId.value}`)
}
</script>

<template>
  <div class="alignment-review-view">
    <div class="alignment-review-view__header">
      <a-button @click="backToWorkspace"><LeftOutlined />返回工作台</a-button>
      <h3 style="margin:0">对齐核查</h3>
      <!-- D2 逃生口：清单本身有误时，改清单要回工作台的物料条重新上传——
           不在这个页面里另起一套清单编辑，避免两处能改同一份清单、口径分裂。
           重新上传后走既有的"重新识别→重新匹配"流程，终局失效语义不变。 -->
      <a-button type="link" @click="backToWorkspace">
        <FileSearchOutlined />清单有误？返回工作台重新上传
      </a-button>
    </div>
    <AnchorReviewMatrix
      v-if="projectId && category"
      :project-id="projectId"
      :category="category"
      :submission-ids="submissionIds"
    />
    <a-empty v-else description="缺少项目或品类参数，请从工作台「对齐核查」按钮进入" />
  </div>
</template>

<style scoped>
.alignment-review-view { padding: 16px 24px; }
.alignment-review-view__header { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
</style>
