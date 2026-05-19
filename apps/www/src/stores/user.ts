import { defineStore } from 'pinia'
import { ref } from 'vue'

const TOKEN_KEY = 'mempas_token'
const USER_KEY = 'mempas_user'

export interface UserInfo {
  username: string
  nickname: string
  role: string
}

export const useUserStore = defineStore('user', () => {
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '')
  const userInfo = ref<UserInfo | null>(
    JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
  )

  function setToken(val: string) {
    token.value = val
    if (val) {
      localStorage.setItem(TOKEN_KEY, val)
    } else {
      localStorage.removeItem(TOKEN_KEY)
    }
  }

  function setUser(info: UserInfo | null) {
    userInfo.value = info
    if (info) {
      localStorage.setItem(USER_KEY, JSON.stringify(info))
    } else {
      localStorage.removeItem(USER_KEY)
    }
  }

  /**
   * Simple login — MEMPAS v1 uses a local admin account.
   * Replace with real API call when backend auth is ready.
   */
  async function login(username: string, password: string) {
    // TODO(auth-integration): replace with POST /api/auth/login when backend is ready
    if (import.meta.env.DEV && username === 'admin' && password === 'admin123') {
      const fakeToken = `mempas-${Date.now()}`
      setToken(fakeToken)
      setUser({ username, nickname: '管理员', role: 'admin' })
      return true
    }
    // Production: delegate to backend (not yet implemented)
    throw new Error('用户名或密码错误')
  }

  function logout() {
    setToken('')
    setUser(null)
  }

  const isLoggedIn = () => !!token.value

  return { token, userInfo, login, logout, isLoggedIn, setToken, setUser }
})
