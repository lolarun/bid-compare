<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { brandTierApi } from '@/api'

/**
 * Brand-tier registration modal.
 *
 * Shown after batch-confirm reports `unknown_brands: string[]`.
 * User assigns 一档/二档/三档 for each unknown brand; component POSTs to brand-tiers API.
 * Emits `done` after all are saved (or skipped).
 */

const props = defineProps<{
  visible: boolean
  brands: string[]
  category?: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
  (e: 'done'): void
}>()

const tiers = ref<Record<string, '一档' | '二档' | '三档'>>({})
const saving = ref(false)

watch(() => props.brands, (b) => {
  const next: Record<string, '一档' | '二档' | '三档'> = {}
  for (const brand of b) {
    next[brand] = tiers.value[brand] || '二档'
  }
  tiers.value = next
}, { immediate: true })

async function save() {
  saving.value = true
  try {
    for (const brand of props.brands) {
      const tier = tiers.value[brand] || '二档'
      try {
        await brandTierApi.create({
          brand_name: brand,
          tier,
          category: props.category || null,
        })
      } catch (e) {
        // Already exists (uniqueness) — silently ignore
        console.warn('brand_tier already exists for', brand, e)
      }
    }
    message.success('品牌档位已写入')
    emit('update:visible', false)
    emit('done')
  } finally {
    saving.value = false
  }
}

function skip() {
  emit('update:visible', false)
  emit('done')
}
</script>

<template>
  <a-modal
    :open="visible"
    title="发现新品牌，请录入档位"
    :width="520"
    :confirm-loading="saving"
    @update:open="(v: boolean) => emit('update:visible', v)"
    @ok="save"
  >
    <template #footer>
      <a-button @click="skip">稍后再说</a-button>
      <a-button type="primary" :loading="saving" @click="save">保存档位</a-button>
    </template>
    <a-alert
      type="warning"
      show-icon
      message="以下品牌未在档位映射表中，建议先指定档位以便后续比价分析"
      style="margin-bottom:12px"
    />
    <a-form layout="horizontal" :label-col="{ span: 8 }">
      <a-form-item v-for="brand in brands" :key="brand" :label="brand">
        <a-radio-group v-model:value="tiers[brand]">
          <a-radio-button value="一档">一档</a-radio-button>
          <a-radio-button value="二档">二档</a-radio-button>
          <a-radio-button value="三档">三档</a-radio-button>
        </a-radio-group>
      </a-form-item>
    </a-form>
    <div style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:8px">
      档位写入后可在「系统设置 → 品牌档位映射」中维护
    </div>
  </a-modal>
</template>
