<script setup lang="ts">
import { ref, reactive } from 'vue'
import { SearchOutlined, ExportOutlined } from '@ant-design/icons-vue'
import type { Dayjs } from 'dayjs'

interface LogRow {
  id: number
  time: string
  user: string
  module: string
  action: string
  target: string
  result: '成功' | '失败'
  remark: string
}

const data = ref<LogRow[]>([
  { id: 1, time: '2026-05-19 09:35:12', user: '杨科', module: '采购价格导入', action: '导入 Excel', target: '桥架报价_江苏华润_202605.xlsx', result: '成功', remark: '新增 36 条，跳过 2 条（重复）' },
  { id: 2, time: '2026-05-19 09:12:45', user: '杨科', module: '招标比价分析', action: '生成横向矩阵', target: '项目 X 给排水材料采购', result: '成功', remark: '5 物料 × 4 供应商' },
  { id: 3, time: '2026-05-19 08:50:21', user: '刘佩珺', module: '物料主数据', action: '编辑物料', target: 'DN100 无缝钢管 (Q235)', result: '成功', remark: '调整推荐品牌为 鞍钢' },
  { id: 4, time: '2026-05-18 17:45:09', user: '刘佩珺', module: '邀标建议', action: '生成邀标清单', target: '项目 Y 数据中心电缆桥架采购', result: '成功', remark: 'AI 推荐 5 家，已选 3 家' },
  { id: 5, time: '2026-05-18 16:20:33', user: '王建波', module: '采购数据分析', action: '查询', target: '关键词「PPR」', result: '成功', remark: '命中 87 条' },
  { id: 6, time: '2026-05-18 15:11:08', user: '杨科', module: '系统设置', action: '更新偏差阈值', target: '桥架类 (yellow:5%, red:10%)', result: '成功', remark: '原值 yellow:8%, red:15%' },
  { id: 7, time: '2026-05-18 11:32:55', user: '杨科', module: '采购价格导入', action: 'OCR 识别', target: '现场扫描件_配电箱.pdf', result: '成功', remark: '识别 12 项，新品牌「正泰电器」' },
  { id: 8, time: '2026-05-17 14:08:42', user: '王建波', module: '供应商管理', action: '查看画像', target: '江苏华润管业', result: '成功', remark: 'AI 评分 92' },
  { id: 9, time: '2026-05-17 10:18:01', user: 'admin', module: '用户管理', action: '停用账号', target: 'chen_old', result: '成功', remark: '员工离职' },
  { id: 10, time: '2026-05-16 09:25:17', user: '刘佩珺', module: '招标比价分析', action: '导出报告', target: '项目 W 暖通改造', result: '失败', remark: '后端 5xx，已重试' },
])

const query = reactive({
  user: undefined as string | undefined,
  module: undefined as string | undefined,
  action: undefined as string | undefined,
  range: undefined as [Dayjs, Dayjs] | undefined,
})

const columns = [
  { title: '时间', dataIndex: 'time', width: 170 },
  { title: '操作人', dataIndex: 'user', width: 90 },
  { title: '模块', dataIndex: 'module', width: 130 },
  { title: '操作', dataIndex: 'action', width: 130 },
  { title: '对象', dataIndex: 'target', ellipsis: true },
  { title: '结果', dataIndex: 'result', width: 80 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
]
</script>

<template>
  <div class="logs-page">
    <div class="logs-page__header">
      <div>
        <h1 class="logs-page__title">操作日志</h1>
        <div class="logs-page__subtitle">系统全量审计日志 · 用户操作、模块、对象与结果可溯</div>
      </div>
      <a-button>
        <template #icon><ExportOutlined /></template>
        导出日志
      </a-button>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space wrap>
        <a-select v-model:value="query.user" placeholder="操作人" allow-clear style="width:140px">
          <a-select-option value="杨科">杨科</a-select-option>
          <a-select-option value="刘佩珺">刘佩珺</a-select-option>
          <a-select-option value="王建波">王建波</a-select-option>
        </a-select>
        <a-select v-model:value="query.module" placeholder="模块" allow-clear style="width:160px">
          <a-select-option value="招标比价分析">招标比价分析</a-select-option>
          <a-select-option value="邀标建议">邀标建议</a-select-option>
          <a-select-option value="物料主数据">物料主数据</a-select-option>
          <a-select-option value="采购数据分析">采购数据分析</a-select-option>
          <a-select-option value="供应商管理">供应商管理</a-select-option>
          <a-select-option value="采购价格导入">采购价格导入</a-select-option>
          <a-select-option value="系统设置">系统设置</a-select-option>
        </a-select>
        <a-select v-model:value="query.action" placeholder="操作类型" allow-clear style="width:140px">
          <a-select-option value="导入">导入</a-select-option>
          <a-select-option value="编辑">编辑</a-select-option>
          <a-select-option value="导出">导出</a-select-option>
          <a-select-option value="查询">查询</a-select-option>
        </a-select>
        <a-range-picker v-model:value="query.range" />
        <a-button type="primary">
          <template #icon><SearchOutlined /></template>
          查询
        </a-button>
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :pagination="{ pageSize: 20, showTotal: (t: number) => `共 ${t} 条` }"
        row-key="id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'result'">
            <a-tag :color="(record as LogRow).result === '成功' ? 'green' : 'red'">
              {{ (record as LogRow).result }}
            </a-tag>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.logs-page {
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
