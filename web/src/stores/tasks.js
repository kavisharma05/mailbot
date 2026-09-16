import { defineStore } from 'pinia'
import api from '../composables/useApi'

export const useTasksStore = defineStore('tasks', {
  state: () => ({
    loading: false,
    tasks: [],
    counts: { all: 0, 'due-soon': 0, missed: 0, undated: 0 },
    statusFilter: 'all',
    categoryFilter: 'all',
    search: '',
  }),
  getters: {
    activeCount: (s) => s.counts.all || s.tasks.filter((t) => t.status === 'open').length,
    dueSoonBadge: (s) => s.counts['due-soon'] || 0,
  },
  actions: {
    async fetchTasks() {
      this.loading = true
      try {
        const { data } = await api.get('/api/tasks', {
          params: {
            status: this.statusFilter,
            category: this.categoryFilter === 'all' ? undefined : this.categoryFilter,
            q: this.search || undefined,
          },
        })
        this.tasks = data.tasks || []
        this.counts = data.counts || this.counts
      } finally {
        this.loading = false
      }
    },
    async markDone(id) {
      await api.put(`/api/tasks/${id}`, { action: 'done' })
      await this.fetchTasks()
    },
    async snooze(id, hours = 2) {
      await api.put(`/api/tasks/${id}`, { action: 'snooze', snooze_hours: hours })
      await this.fetchTasks()
    },
    async createTask(payload) {
      await api.post('/api/tasks', payload)
      await this.fetchTasks()
    },
    async reopen(id) {
      await api.put(`/api/tasks/${id}`, { action: 'reopen' })
      await this.fetchTasks()
    },
  },
})
