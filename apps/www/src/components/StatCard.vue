<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{
  icon?: Component
  iconBg?: string
  label: string
  value: string | number
  unit?: string
  trend?: { value: string; positive?: boolean; danger?: boolean; label?: string }
}>()
</script>

<template>
  <div class="stat-card">
    <div v-if="icon" class="stat-card__icon" :style="{ background: iconBg || 'rgba(22,119,255,0.1)' }">
      <component :is="icon" />
    </div>
    <div class="stat-card__body">
      <div class="stat-card__label">{{ label }}</div>
      <div class="stat-card__value">
        {{ value }}
        <span v-if="unit" class="stat-card__unit">{{ unit }}</span>
      </div>
      <div v-if="trend" class="stat-card__trend">
        <span
          class="stat-card__trend-value"
          :class="{
            'is-up': trend.positive && !trend.danger,
            'is-down': !trend.positive && !trend.danger,
            'is-danger': trend.danger,
          }"
        >
          {{ trend.value }}
        </span>
        <span class="stat-card__trend-label">{{ trend.label || '较上月' }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.stat-card {
  background: #fff;
  border-radius: @border-radius-lg;
  padding: 18px 20px;
  box-shadow: @shadow-1;
  display: flex;
  gap: 14px;
  align-items: flex-start;
  transition: all 0.2s;

  &:hover {
    box-shadow: @shadow-2;
    transform: translateY(-1px);
  }

  &__icon {
    width: 40px;
    height: 40px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    color: @primary-color;
    flex-shrink: 0;
  }

  &__body {
    flex: 1;
    min-width: 0;
  }

  &__label {
    font-size: 13px;
    color: @text-color-secondary;
    margin-bottom: 4px;
  }

  &__value {
    font-size: 26px;
    font-weight: 700;
    color: @heading-color;
    line-height: 1.2;
    letter-spacing: 0.5px;
  }

  &__unit {
    font-size: 13px;
    font-weight: 400;
    color: @text-color-secondary;
    margin-left: 2px;
  }

  &__trend {
    margin-top: 8px;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 4px;
  }

  &__trend-value {
    &.is-up { color: @alert-normal-color; }
    &.is-down { color: @alert-yellow-color; }
    &.is-danger { color: @alert-red-color; }
  }

  &__trend-label {
    color: @text-color-tertiary;
  }
}
</style>
