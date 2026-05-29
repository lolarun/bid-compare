<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { brandTierApi } from '@/api'

/**
 * Brand-tier registration modal.
 *
 * Audit-driven design (Phase 3 → audit-fix F):
 *
 * - **No silent default**: tier values start as `undefined`. The 保存 button
 *   is disabled until every brand has an explicit selection. Previously
 *   `'二档'` was assigned implicitly, so users could "save" without
 *   actually deciding anything.
 *
 * - **Parallel save with error surface**: previous version did serial awaits
 *   in a for-loop AND swallowed all errors into `console.warn`. Now uses
 *   Promise.allSettled and surfaces partial failures via `message.error`.
 *
 * - **Distinct cancel vs done events**: `cancel` for "稍后再说" (no DB write),
 *   `done` only after a successful save. Parents can react accordingly.
 */

const props = defineProps<{
  visible: boolean
  brands: string[]
  category?: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
  (e: 'done'): void
  (e: 'cancel'): void
}>()

// AUDIT-FIX C2: undefined-by-default; user MUST pick
const tiers = ref<Record<string, '国产' | '合资' | '三档' | undefined>>({})
const saving = ref(false)

watch(() => props.brands, (b) => {
  const next: Record<string, '国产' | '合资' | '三档' | undefined> = {}
  for (const brand of b) {
    next[brand] = tiers.value[brand]  // preserve prior selection if any
  }
  tiers.value = next
}, { immediate: true })

const allChosen = computed(() => props.brands.every((b) => !!tiers.value[b]))

async function save() {
  if (!allChosen.value) {
    message.warning('请为每个品牌选择档位后再保存')
    return
  }
  saving.value = true
  try {
    const results = await Promise.allSettled(
      props.brands.map((brand) =>
        brandTierApi.create({
          brand_name: brand,
          tier: tiers.value[brand]!,
          category: props.category || null,
        })
      )
    )
    const failures: string[] = []
    results.forEach((r, i) => {
      if (r.status === 'rejected') {
        // Duplicate brand_name returning 409/400 is acceptable — the row
        // already exists. Surface only "true" failures (network/5xx).
        const status = (r.reason as { response?: { status?: number } })?.response?.status
        if (status === undefined || status >= 500) {
          failures.push(props.brands[i])
        }
      }
    })
    if (failures.length === 0) {
      message.success(`已保存 ${props.brands.length} 个品牌的档位`)
      emit('update:visible', false)
      emit('done')
    } else {
      message.error(
        `${failures.length}/${props.brands.length} 个品牌写入失败：${failures.join('、')}`
      )
    }
  } finally {
    saving.value = false
  }
}

function cancel() {
  emit('update:visible', false)
  emit('cancel')
}
</script>

<template>
  <a-modal
    :open="visible"
    title="发现新品牌，请录入档位"
    :width="520"
    :confirm-loading="saving"
    :mask-closable="false"
    @update:open="(v: boolean) => emit('update:visible', v)"
    @cancel="cancel"
  >
    <template #footer>
      <a-button @click="cancel">稍后再说</a-button>
      <a-button type="primary" :loading="saving" :disabled="!allChosen" @click="save">
        保存档位
      </a-button>
    </template>
    <a-alert
      type="warning"
      show-icon
      message="以下品牌未在档位映射表中，请为每个品牌指定档位"
      style="margin-bottom:12px"
    />
    <a-form layout="horizontal" :label-col="{ span: 8 }">
      <a-form-item v-for="brand in brands" :key="brand" :label="brand">
        <a-radio-group v-model:value="tiers[brand]">
          <a-radio-button value="国产">国产</a-radio-button>
          <a-radio-button value="合资">合资</a-radio-button>
          <a-radio-button value="三档">三档</a-radio-button>
        </a-radio-group>
      </a-form-item>
    </a-form>
    <div v-if="!allChosen" style="font-size:12px;color:rgba(255,77,79,0.85);margin-top:8px">
      ⚠ 请为每个品牌选择档位后才能保存
    </div>
    <div v-else style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:8px">
      档位写入后可在「系统设置 → 品牌档位映射」中维护
    </div>
  </a-modal>
</template>
