<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  BellOutlined,
  FullscreenOutlined,
  FullscreenExitOutlined,
  UserOutlined,
  LogoutOutlined,
  SettingOutlined,
  SearchOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons-vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { ref } from 'vue'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const userStore = useUserStore()

const breadcrumbs = computed(() => {
  const crumbs: { title: string; path?: string }[] = []
  const group = route.meta?.group as string | undefined
  if (group) crumbs.push({ title: group })
  if (route.meta?.title) crumbs.push({ title: route.meta.title as string })
  return crumbs
})

const isFullscreen = ref(false)

async function toggleFullscreen() {
  if (!document.fullscreenElement) {
    await document.documentElement.requestFullscreen()
    isFullscreen.value = true
  } else {
    await document.exitFullscreen()
    isFullscreen.value = false
  }
}

function handleLogout() {
  userStore.logout()
  router.push('/login')
}
</script>

<template>
  <a-layout-header class="header-view">
    <div class="header-view__left">
      <component
        :is="appStore.collapsed ? MenuUnfoldOutlined : MenuFoldOutlined"
        class="header-view__trigger"
        @click="appStore.toggleCollapsed"
      />
      <a-breadcrumb class="header-view__breadcrumb">
        <a-breadcrumb-item v-for="(b, idx) in breadcrumbs" :key="idx">
          {{ b.title }}
        </a-breadcrumb-item>
      </a-breadcrumb>
    </div>

    <div class="header-view__search">
      <a-input
        placeholder="搜索材料、供应商、历史记录..."
        size="middle"
        allow-clear
      >
        <template #prefix>
          <SearchOutlined style="color: rgba(0,0,0,0.35)" />
        </template>
      </a-input>
    </div>

    <div class="header-view__right">
      <a-select :value="'admin'" size="small" class="header-view__role" :bordered="false">
        <a-select-option value="admin">系统管理员</a-select-option>
        <a-select-option value="buyer">比价员</a-select-option>
        <a-select-option value="viewer">查看者</a-select-option>
      </a-select>

      <a-tooltip title="帮助中心">
        <QuestionCircleOutlined class="header-view__icon" @click="router.push('/help')" />
      </a-tooltip>

      <a-tooltip :title="isFullscreen ? '退出全屏' : '全屏'">
        <component
          :is="isFullscreen ? FullscreenExitOutlined : FullscreenOutlined"
          class="header-view__icon"
          @click="toggleFullscreen"
        />
      </a-tooltip>

      <a-badge :count="3" :offset="[-2, 4]">
        <BellOutlined class="header-view__icon" />
      </a-badge>

      <a-dropdown>
        <span class="header-view__avatar">
          <a-avatar :size="28" style="background-color: #1677ff">
            <template #icon><UserOutlined /></template>
          </a-avatar>
          <span class="header-view__username">{{ userStore.userInfo?.nickname || '管理员' }}</span>
        </span>
        <template #overlay>
          <a-menu>
            <a-menu-item key="profile">
              <UserOutlined />
              <span style="margin-left: 8px">个人中心</span>
            </a-menu-item>
            <a-menu-item key="settings" @click="router.push('/system/settings')">
              <SettingOutlined />
              <span style="margin-left: 8px">系统设置</span>
            </a-menu-item>
            <a-menu-divider />
            <a-menu-item key="logout" @click="handleLogout">
              <LogoutOutlined />
              <span style="margin-left: 8px">退出登录</span>
            </a-menu-item>
          </a-menu>
        </template>
      </a-dropdown>
    </div>
  </a-layout-header>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.header-view {
  background: @layout-header-bg;
  padding: 0 16px;
  height: @header-height;
  line-height: @header-height;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  position: sticky;
  top: 0;
  z-index: 9;

  &__left {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  &__trigger {
    font-size: 16px;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
    color: @text-color-secondary;
    transition: background 0.2s;
  }
  &__trigger:hover {
    background: rgba(0, 0, 0, 0.04);
    color: @primary-color;
  }

  &__breadcrumb {
    font-size: 13px;
  }

  &__search {
    flex: 1;
    max-width: 360px;

    :deep(.ant-input-affix-wrapper) {
      background: #f5f5f0;
      border-color: transparent;
      border-radius: 16px;
    }
  }

  &__right {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-left: auto;
  }

  &__role {
    width: 120px;

    :deep(.ant-select-selector) {
      background: transparent !important;
      color: @text-color-secondary;
    }
  }

  &__icon {
    font-size: 16px;
    color: @text-color-secondary;
    cursor: pointer;
    padding: 4px;
    border-radius: 4px;
    transition: all 0.2s;
  }
  &__icon:hover {
    color: @primary-color;
    background: rgba(0, 0, 0, 0.04);
  }

  &__avatar {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
    transition: background 0.2s;
    line-height: 1;
  }
  &__avatar:hover {
    background: rgba(0, 0, 0, 0.04);
  }

  &__username {
    font-size: 13px;
    color: @text-color;
  }
}
</style>
