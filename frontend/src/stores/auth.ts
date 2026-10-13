import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

interface UserInfo {
  userId: string
  role: 'worker' | 'supervisor' | 'admin'
  tenantId: string
  username?: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('fieldops-agent-token'))
  const user = ref<UserInfo | null>((() => {
    try {
      return JSON.parse(localStorage.getItem('fieldops-agent-user') ?? 'null')
    } catch {
      return null
    }
  })())

  const isLoggedIn = computed(() => !!token.value)
  const isSupervisor = computed(() =>
    user.value?.role === 'supervisor' || user.value?.role === 'admin'
  )

  function login(accessToken: string, userInfo: UserInfo) {
    token.value = accessToken
    user.value = userInfo
    localStorage.setItem('fieldops-agent-token', accessToken)
    localStorage.setItem('fieldops-agent-user', JSON.stringify(userInfo))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('fieldops-agent-token')
    localStorage.removeItem('fieldops-agent-user')
  }

  return { token, user, isLoggedIn, isSupervisor, login, logout }
})
