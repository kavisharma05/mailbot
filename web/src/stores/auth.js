import { defineStore } from 'pinia'
import api from '../composables/useApi'

const TOKEN_KEY = 'mailbot_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null,
    loaded: false,
  }),
  getters: {
    isLoggedIn: (s) => Boolean(s.token),
  },
  actions: {
    setSession(token, user) {
      this.token = token
      this.user = user
      localStorage.setItem(TOKEN_KEY, token)
      api.defaults.headers.common.Authorization = `Bearer ${token}`
    },
    clearSession() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      delete api.defaults.headers.common.Authorization
    },
    async bootstrap() {
      if (!this.token) {
        this.loaded = true
        return
      }
      api.defaults.headers.common.Authorization = `Bearer ${this.token}`
      try {
        const { data } = await api.get('/api/auth/me')
        this.user = data.user
      } catch {
        this.clearSession()
      } finally {
        this.loaded = true
      }
    },
    async requestCode(username) {
      const { data } = await api.post('/api/auth/request-code', { username })
      return data
    },
    async verify(username, code) {
      const { data } = await api.post('/api/auth/verify', { username, code })
      this.setSession(data.token, data.user)
      return data
    },
    async logout() {
      try {
        await api.post('/api/auth/logout')
      } catch {
        /* ignore */
      }
      this.clearSession()
    },
    async gmailConnectUrl() {
      const { data } = await api.get('/api/auth/gmail/connect-url')
      return data.url
    },
  },
})
