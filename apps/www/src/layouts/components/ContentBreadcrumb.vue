<!--
  面包屑（2026-09-03，用户决策）：从页眉挪到主内容区顶部。放在 BasicLayout 里
  统一渲染，而不是每个页面自己写一遍——14 个页面各写一份必然长歪。

  层级取 首页 / meta.group / meta.title。`hideInMenu` 的深层页（项目概述、
  比价工作台）没有 meta.group，用 useAppMenu 的前缀匹配补出父入口，
  面包屑因此能显示 首页 / 业务功能 / 招标比价 / 项目概述。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { HomeOutlined } from '@ant-design/icons-vue'
import { useAppMenu } from '@/composables/useAppMenu'

const route = useRoute()
const router = useRouter()
const { activeItem } = useAppMenu()

interface Crumb {
  title: string
  path?: string
}

const crumbs = computed<Crumb[]>(() => {
  const out: Crumb[] = []
  const parent = activeItem.value
  const group = (route.meta?.group as string | undefined) ?? parent?.group
  if (group) out.push({ title: group })
  // 父入口自己就是当前页时不重复一遍（/workspace 上 parent.title === meta.title）
  if (parent && parent.key !== route.path) out.push({ title: parent.title, path: parent.key })
  if (route.meta?.title) out.push({ title: route.meta.title as string })
  return out
})
</script>

<template>
  <a-breadcrumb class="content-breadcrumb">
    <a-breadcrumb-item>
      <a @click.prevent="router.push('/dashboard')">
        <HomeOutlined />
        <span class="content-breadcrumb__home">首页</span>
      </a>
    </a-breadcrumb-item>
    <a-breadcrumb-item v-for="(c, idx) in crumbs" :key="idx">
      <a v-if="c.path" @click.prevent="router.push(c.path!)">{{ c.title }}</a>
      <span v-else>{{ c.title }}</span>
    </a-breadcrumb-item>
  </a-breadcrumb>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.content-breadcrumb {
  padding: 12px 24px 0;
  font-size: 13px;

  &__home {
    margin-left: 4px;
  }
}
</style>
