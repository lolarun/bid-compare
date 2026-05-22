<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { message } from 'ant-design-vue'
import { UserOutlined, LockOutlined } from '@ant-design/icons-vue'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const loading = ref(false)
const loginError = ref(false)

const form = reactive({
  username: '',
  password: '',
  remember: true,
})

async function handleSubmit() {
  if (!form.username || !form.password) {
    message.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  loginError.value = false
  try {
    await userStore.login(form.username, form.password)
    message.success('登录成功')
    const redirect = (route.query.redirect as string) || '/dashboard'
    router.replace(decodeURIComponent(redirect))
  } catch (e: any) {
    loginError.value = true
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrapper">
    <div class="login-container">
      <!-- Header -->
      <div class="login-top">
        <div class="login-header">
          <img src="@/assets/logo.svg" alt="logo" class="login-logo" />
          <span class="login-title">MEMPAS</span>
        </div>
        <div class="login-desc">机电材料查询比价分析系统</div>
      </div>

      <!-- Form -->
      <div class="login-main">
        <a-alert
          v-if="loginError"
          type="error"
          message="用户名或密码错误"
          show-icon
          closable
          style="margin-bottom: 24px"
          @close="loginError = false"
        />

        <a-form layout="vertical" @submit.prevent="handleSubmit">
          <a-form-item>
            <a-input
              v-model:value="form.username"
              size="large"
              placeholder="用户名"
              @press-enter="handleSubmit"
            >
              <template #prefix>
                <UserOutlined style="color: rgba(0, 0, 0, 0.25)" />
              </template>
            </a-input>
          </a-form-item>

          <a-form-item>
            <a-input-password
              v-model:value="form.password"
              size="large"
              placeholder="密码"
              @press-enter="handleSubmit"
            >
              <template #prefix>
                <LockOutlined style="color: rgba(0, 0, 0, 0.25)" />
              </template>
            </a-input-password>
          </a-form-item>

          <a-form-item>
            <a-checkbox v-model:checked="form.remember">记住密码</a-checkbox>
          </a-form-item>

          <a-form-item>
            <a-button
              type="primary"
              size="large"
              html-type="submit"
              :loading="loading"
              block
            >
              登录
            </a-button>
          </a-form-item>
        </a-form>
      </div>

      <!-- Footer -->
      <div class="login-footer">
        <div class="login-copyright">
          MEMPAS &copy; 2025 上海建工一建
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-wrapper {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #f0f5ff 0%, #e6f4ff 50%, #f0f5ff 100%);
}

.login-container {
  width: 100%;
  max-width: 400px;
  padding: 0 24px;
}

/* ─── Top ────────────────────────────────────────────────────────────── */
.login-top {
  text-align: center;
  margin-bottom: 40px;
}

.login-header {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
}

.login-logo {
  width: 44px;
  height: 44px;
}

.login-title {
  font-size: 32px;
  font-weight: 700;
  color: rgba(0, 0, 0, 0.85);
  letter-spacing: 2px;
}

.login-desc {
  margin-top: 12px;
  color: rgba(0, 0, 0, 0.45);
  font-size: 14px;
}

/* ─── Main form card ─────────────────────────────────────────────────── */
.login-main {
  background: #fff;
  border-radius: 8px;
  padding: 32px 32px 8px;
  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.06);
}

/* ─── Footer ─────────────────────────────────────────────────────────── */
.login-footer {
  text-align: center;
  margin-top: 48px;
}

.login-copyright {
  color: rgba(0, 0, 0, 0.35);
  font-size: 13px;
}
</style>
