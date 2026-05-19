<script setup lang="ts">
defineSlots<{ default: () => unknown }>()
</script>

<template>
  <a-layout-content class="wrap-content">
    <slot />
  </a-layout-content>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.wrap-content {
  margin: 16px;
  background: transparent;
  min-height: calc(100vh - @header-height - 32px);
  overflow: auto;
}
</style>
