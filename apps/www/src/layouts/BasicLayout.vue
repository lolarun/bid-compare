<script setup lang="ts">
import { useAppStore } from '@/stores/app'
import SiderMenu from './components/SiderMenu.vue'
import HeaderView from './components/HeaderView.vue'

const appStore = useAppStore()
</script>

<template>
  <a-layout class="basic-layout">
    <SiderMenu :collapsed="appStore.collapsed" />
    <a-layout
      class="basic-layout__main"
      :class="{ 'basic-layout__main--collapsed': appStore.collapsed }"
    >
      <HeaderView />
      <a-layout-content class="basic-layout__content">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </a-layout-content>
      <a-layout-footer class="basic-layout__footer">
        MEMPAS 机电材料查询比价分析系统 &copy; 2025 上海建工一建
      </a-layout-footer>
    </a-layout>
  </a-layout>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.basic-layout {
  min-height: 100vh;
  background: @layout-body-bg;

  &__main {
    margin-left: @sider-width;
    transition: margin-left 0.2s;
    background: @layout-body-bg;

    &--collapsed {
      margin-left: @sider-collapsed-width;
    }
  }

  &__content {
    padding: 16px;
    background: @layout-body-bg;
    min-height: calc(100vh - @header-height - 44px);
  }

  &__footer {
    text-align: center;
    padding: 12px 24px;
    color: @text-color-tertiary;
    font-size: 12px;
    background: @layout-body-bg;
  }
}
</style>
