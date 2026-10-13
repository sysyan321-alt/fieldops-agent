import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

interface UserInfo {
  userId: string
  role: 'caregiver' | 'supervisor' | 'admin'
  tenantId: string
  username?: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('care-agent-token'))
  const user = ref<UserInfo | null>((() => {
    try {
      return JSON.parse(localStorage.getItem('care-agent-user') ?? 'null')
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
    localStorage.setItem('care-agent-token', accessToken)
    localStorage.setItem('care-agent-user', JSON.stringify(userInfo))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('care-agent-token')
    localStorage.removeItem('care-agent-user')
  }

  return { token, user, isLoggedIn, isSupervisor, login, logout }
})
