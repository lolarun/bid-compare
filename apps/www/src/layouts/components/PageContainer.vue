<script setup lang="ts">
defineProps<{
  title?: string
  subTitle?: string
  ghost?: boolean
}>()
defineSlots<{
  default: () => unknown
  extra: () => unknown
  tags: () => unknown
  footer: () => unknown
}>()
</script>

<template>
  <div class="page-container" :class="{ 'page-container--ghost': ghost }">
    <header v-if="title || $slots.extra || $slots.tags" class="page-container__header">
      <div class="page-container__title-block">
        <h1 v-if="title" class="page-container__title">
          {{ title }}
          <span v-if="subTitle" class="page-container__sub-title">{{ subTitle }}</span>
        </h1>
        <div v-if="$slots.tags" class="page-container__tags"><slot name="tags" /></div>
      </div>
      <div v-if="$slots.extra" class="page-container__extra"><slot name="extra" /></div>
    </header>
    <div class="page-container__body">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="page-container__footer"><slot name="footer" /></footer>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.page-container {
  background: @component-background;
  border-radius: @border-radius-lg;
  padding: 20px 24px;
  box-shadow: @shadow-1;
  min-height: calc(100vh - @header-height - 32px);

  &--ghost {
    background: transparent;
    box-shadow: none;
    padding: 0;
  }

  &__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 16px;
  }

  &__title-block {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  &__title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: @heading-color;
    line-height: 1.4;
  }

  &__sub-title {
    margin-left: 8px;
    font-size: 13px;
    font-weight: 400;
    color: @text-color-secondary;
  }

  &__tags {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  &__extra {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }

  &__body {
    min-height: 200px;
  }

  &__footer {
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid @border-color-split;
  }
}
</style>
