<!--
  侧边导航：**全部菜单都在这里**（分组一次铺完），顶栏不放任何导航。

  混合布局（2026-09-04 用户按范例定案）：顶栏通栏且承载 Logo，所以侧栏从
  顶栏下方开始（`top: @header-height`），自己不再画 Logo 块。
  2026-09-03 那版"把一级分组搬进顶栏"是理解错了，已收回。

  菜单始终由路由表推导（`useAppMenu` 读 `router.getRoutes()`），没有第二份
  菜单配置：加一个页面只需在 router/index.ts 写 meta.title + meta.group。
-->
<script setup lang="ts">
import { computed, h, type VNode } from 'vue'
import * as Icons from '@ant-design/icons-vue'
import { MenuFoldOutlined, MenuUnfoldOutlined } from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import { useAppMenu } from '@/composables/useAppMenu'
import { useAppStore } from '@/stores/app'

defineProps<{
  collapsed: boolean
}>()

const router = useRouter()
const appStore = useAppStore()
const { groups, selectedKeys } = useAppMenu()

function renderIcon(name?: string): VNode {
  if (!name) return h(Icons.AppstoreOutlined)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const C = (Icons as any)[name]
  return C ? h(C) : h(Icons.AppstoreOutlined)
}

/** 分组标题 + 叶子，一次铺完。 */
const menuItems = computed(() =>
  groups.value.map((group) => ({
    key: `group-${group.title}`,
    type: 'group' as const,
    label: group.title,
    children: group.items.map((it) => ({
      key: it.key,
      label: it.title,
      icon: () => renderIcon(it.icon),
    })),
  })),
)

function handleClick({ key }: { key: string }) {
  router.push(key)
}
</script>

<template>
  <a-layout-sider
    :collapsed="collapsed"
    :trigger="null"
    :width="220"
    :collapsed-width="64"
    theme="light"
    class="sider-menu"
  >
    <a-menu
      mode="inline"
      theme="light"
      :selected-keys="selectedKeys"
      :items="menuItems"
      class="sider-menu__list"
      @click="handleClick"
    />

    <!-- 底部折叠钮（2026-09-04 用户要求）：跟顶栏那个是同一个 store 开关，
         不是第二套状态——两处点哪个都一样。 -->
    <button
      type="button"
      class="sider-menu__collapse"
      :class="{ 'sider-menu__collapse--collapsed': collapsed }"
      :title="collapsed ? '展开菜单' : '收起菜单'"
      @click="appStore.toggleCollapsed"
    >
      <component :is="collapsed ? MenuUnfoldOutlined : MenuFoldOutlined" />
    </button>
  </a-layout-sider>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.sider-menu {
  background: @layout-sider-bg;
  position: fixed;
  left: 0;
  top: @header-height;   // 顶栏通栏，侧栏从它下面开始
  bottom: 0;
  z-index: 10;
  overflow: hidden auto;
  border-right: 1px solid @border-color-split;











  &__list {
    background: @layout-sider-bg;
    border-right: none;
    padding: 8px 0;
    // 给底部折叠条留位置，否则菜单长了会被压在按钮下面看不见
    padding-bottom: 48px;
  }

  // 只放图标，不带文字（照用户给的范例：左下角一个小图标）
  &__collapse {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding-left: 22px;
    font-size: 16px;
    border: none;
    border-top: 1px solid @border-color-split;
    background: @layout-sider-bg;
    color: @text-color-secondary;
    font-size: 13px;
    cursor: pointer;
    transition: all 0.2s;

    &:hover {
      color: @primary-color;
      background: @sider-item-hover-bg;
    }
  }

  // 折叠态没有文字可对齐，图标居中
  &__collapse--collapsed {
    justify-content: center;
    padding-left: 0;
  }
}

.sider-fade-enter-active,
.sider-fade-leave-active {
  transition: opacity 0.2s ease;
}
.sider-fade-enter-from,
.sider-fade-leave-to {
  opacity: 0;
}
</style>
