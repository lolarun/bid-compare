import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '@/api/client'

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

  async function login(username: string, password: string) {
    const { data } = await api.post<{
      access_token: string
      username: string
      role: string
    }>('/auth/login', { username, password })
    setToken(data.access_token)
    setUser({ username: data.username, nickname: data.role, role: data.role })
    return true
  }

  function logout() {
    setToken('')
    setUser(null)
  }

  const isLoggedIn = () => !!token.value

  return { token, userInfo, login, logout, isLoggedIn, setToken, setUser }
})
