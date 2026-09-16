import { defineStore } from 'pinia'
import api from '../composables/useApi'
import { todayIST } from '../composables/useDate'

export const useScheduleStore = defineStore('schedule', {
  state: () => ({
    loading: false,
    date: todayIST(),
    sessions: [],
    section: null,
    cacheFresh: false,
  }),
  actions: {
    async fetchDay(day, refresh = false) {
      this.loading = true
      try {
        const today = todayIST()
      const path = day === today ? '/api/schedule/today' : `/api/schedule/${day}`
        const { data } = await api.get(path, { params: refresh ? { refresh: true } : {} })
        this.date = data.date
        this.sessions = data.sessions || []
        this.section = data.section
        this.cacheFresh = data.cache_fresh
      } finally {
        this.loading = false
      }
    },
    async forceRefresh() {
      this.loading = true
      try {
        const { data } = await api.post('/api/schedule/refresh')
        this.date = data.date
        this.sessions = data.sessions || []
        this.section = data.section
      } finally {
        this.loading = false
      }
    },
  },
})
