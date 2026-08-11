<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

function goHome() {
  router.push('/dashboard')
}
</script>

<template>
  <div class="forbidden-page">
    <a-result
      status="403"
      title="403"
      sub-title="抱歉，您没有权限访问此页面。"
    >
      <template #extra>
        <a-button type="primary" @click="goHome">返回首页</a-button>
      </template>
    </a-result>
    <div v-if="userStore.userInfo" class="forbidden-page__info">
      <p>当前用户：{{ userStore.userInfo.nickname }}（{{ userStore.userInfo.role }}）</p>
      <p>如需提升权限，请联系系统管理员。</p>
    </div>
  </div>
</template>

<style scoped lang="less">
.forbidden-page {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 70vh;
  text-align: center;

  &__info {
    margin-top: 16px;
    color: rgba(0, 0, 0, 0.45);
    font-size: 13px;
    line-height: 1.8;
  }
}
</style>
