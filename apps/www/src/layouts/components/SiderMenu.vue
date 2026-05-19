<script setup lang="ts">
import { computed, h, type VNode } from 'vue'
import { useRoute, useRouter, type RouteRecordRaw } from 'vue-router'
import * as Icons from '@ant-design/icons-vue'

const props = defineProps<{
  collapsed: boolean
}>()

const router = useRouter()
const route = useRoute()

interface MenuItem {
  key: string
  title: string
  icon?: string
  group: string
}

interface MenuGroup {
  title: string
  items: MenuItem[]
}

const GROUP_ORDER = ['工作台', '业务功能', '数据管理', '系统管理'] as const

const groups = computed<MenuGroup[]>(() => {
  const layoutRoute = router.getRoutes().find((r) => r.name === 'Layout')
  if (!layoutRoute) return []
  const children = collectLeafRoutes(layoutRoute.children ?? [])
  const map = new Map<string, MenuItem[]>()
  for (const r of children) {
    if (!r.meta?.title || r.meta?.hideInMenu) continue
    const group = (r.meta.group as string) || '其他'
    const path = r.path.startsWith('/') ? r.path : `/${r.path}`
    const item: MenuItem = {
      key: path,
      title: r.meta.title as string,
      icon: r.meta.icon as string | undefined,
      group,
    }
    const list = map.get(group) ?? []
    list.push(item)
    map.set(group, list)
  }
  const ordered = [...GROUP_ORDER, ...[...map.keys()].filter((g) => !GROUP_ORDER.includes(g as typeof GROUP_ORDER[number]))]
  return ordered
    .filter((g) => map.has(g))
    .map((g) => ({ title: g, items: map.get(g)! }))
})

function collectLeafRoutes(routes: readonly RouteRecordRaw[]): RouteRecordRaw[] {
  const out: RouteRecordRaw[] = []
  for (const r of routes) {
    if (r.children && r.children.length > 0) {
      out.push(...collectLeafRoutes(r.children))
    } else {
      out.push(r)
    }
  }
  return out
}

const selectedKeys = computed(() => [route.path])

function renderIcon(name?: string): VNode {
  if (!name) return h(Icons.AppstoreOutlined)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const C = (Icons as any)[name]
  return C ? h(C) : h(Icons.AppstoreOutlined)
}

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
    <div class="sider-menu__logo">
      <div class="sider-menu__logo-icon">M</div>
      <transition name="sider-fade">
        <div v-if="!collapsed" class="sider-menu__logo-text">
          <div class="sider-menu__logo-title">MEMPAS</div>
          <div class="sider-menu__logo-subtitle">机材比价</div>
        </div>
      </transition>
    </div>
    <a-menu
      mode="inline"
      theme="light"
      :selected-keys="selectedKeys"
      :items="menuItems"
      :inline-collapsed="collapsed"
      class="sider-menu__list"
      @click="handleClick"
    />
  </a-layout-sider>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.sider-menu {
  background: @layout-sider-bg;
  height: 100vh;
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
  border-right: 1px solid @border-color-split;

  &__logo {
    height: @header-height;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 16px;
    border-bottom: 1px solid @border-color-split;
    white-space: nowrap;
  }

  &__logo-icon {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    background: @primary-color;
    color: #fff;
    font-size: 18px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  &__logo-text {
    display: flex;
    flex-direction: column;
    line-height: 1.2;
    overflow: hidden;
  }

  &__logo-title {
    font-size: 15px;
    font-weight: 700;
    color: @heading-color;
    letter-spacing: 0.5px;
  }

  &__logo-subtitle {
    font-size: 11px;
    color: @text-color-tertiary;
  }

  &__list {
    background: @layout-sider-bg;
    border-right: none;
    padding: 8px 0;
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

