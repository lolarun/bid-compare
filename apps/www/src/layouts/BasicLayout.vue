<!--
  混合布局（2026-09-04 用户按范例定案）：**顶栏通栏**（Logo 在顶栏左侧，
  但顶栏不放任何导航）+ **左侧完整菜单**（全部分组一次铺完，折叠钮在侧栏底部）。

  ⚠ 与 2026-09-03 那版"混合布局"的区别：那版把一级分组也搬进了顶栏、侧栏只留
  当前分组的叶子——不是用户要的。菜单**全部留在侧栏**，顶栏只承载 Logo/搜索/用户区。

  面包屑在内容区顶部（ContentBreadcrumb），不在顶栏里。
-->
<script setup lang="ts">
import { useAppStore } from '@/stores/app'
import SiderMenu from './components/SiderMenu.vue'
import HeaderView from './components/HeaderView.vue'
import ContentBreadcrumb from './components/ContentBreadcrumb.vue'

const appStore = useAppStore()
</script>

<template>
  <a-layout class="basic-layout">
    <HeaderView />
    <a-layout class="basic-layout__body">
      <SiderMenu :collapsed="appStore.collapsed" />
      <a-layout
        class="basic-layout__main"
        :class="{ 'basic-layout__main--collapsed': appStore.collapsed }"
      >
        <ContentBreadcrumb />
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
  </a-layout>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.basic-layout {
  min-height: 100vh;
  background: @layout-body-bg;

  &__body {
    background: @layout-body-bg;
  }

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
