import { defineStore } from 'pinia'
import api from '../composables/useApi'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    loading: false,
    error: null,
    data: null,
    syncPulse: false,
  }),
  getters: {
    stats: (s) => s.data?.stats || { deadlines_due: 0, classes_today: 0, placement: 0 },
    urgentTask: (s) => s.data?.urgent_task,
    todayClasses: (s) => s.data?.today_classes || [],
    upcoming: (s) => s.data?.upcoming_deadlines || [],
    activity: (s) => s.data?.activity?.items || s.data?.activity || [],
    greetingName: (s) => s.data?.greeting_name || 'there',
    section: (s) => s.data?.section,
    dbOk: (s) => s.data?.db_ok !== false,
  },
  actions: {
    async fetchDashboard() {
      this.loading = true
      this.error = null
      try {
        const { data } = await api.get('/api/dashboard')
        this.data = data
        if (data?.db_ok === false) {
          this.error = 'Could not reach the database. Retry in a moment.'
        }
      } catch {
        this.error = 'Could not load your dashboard.'
      } finally {
        this.loading = false
        this.syncPulse = false
      }
    },
    setSyncPulse(v) {
      this.syncPulse = v
      if (v) this.fetchDashboard()
    },
  },
})
