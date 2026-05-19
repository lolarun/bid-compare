<script setup lang="ts">
defineProps<{
  title: string
  subTitle?: string
}>()
</script>

<template>
  <div class="page-header">
    <div class="page-header__left">
      <h1 class="page-header__title"><slot name="title">{{ title }}</slot></h1>
      <div v-if="$slots.subTitle || subTitle" class="page-header__subtitle">
        <slot name="subTitle">{{ subTitle }}</slot>
      </div>
    </div>
    <div v-if="$slots.extra" class="page-header__extra">
      <slot name="extra" />
    </div>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;

  &__left { flex: 1; }

  &__title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: @heading-color;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__subtitle {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }

  &__extra {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
    margin-left: 16px;
  }
}
</style>
