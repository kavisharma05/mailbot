import { defineStore } from 'pinia'
import api from '../composables/useApi'

export const useLibraryStore = defineStore('library', {
  state: () => ({
    loading: false,
    overview: null,
    resources: [],
    publications: [],
    analysis: null,
    refreshing: false,
    briefs: {},
    briefLoading: {},
  }),
  getters: {
    categories: (s) => s.overview?.categories || [],
    resourceCount: (s) => s.overview?.resource_count || 0,
    lastDailyRun: (s) => s.overview?.last_daily_run_on || null,
  },
  actions: {
    async fetchOverview() {
      this.loading = true
      try {
        const { data } = await api.get('/api/library/overview')
        this.overview = data
      } finally {
        this.loading = false
      }
    },
    async fetchResources(q = '') {
      const { data } = await api.get('/api/library/resources', { params: q ? { q } : {} })
      this.resources = data.categories || []
      return data
    },
    async fetchPublications(kind = null) {
      const { data } = await api.get('/api/library/publications', {
        params: kind ? { kind } : {},
      })
      this.publications = data.items || []
    },
    async fetchAnalysis() {
      const { data } = await api.get('/api/library/analysis')
      this.analysis = data
    },
    async fetchStoryBrief(item) {
      const key = `${item.kind}|${item.url}`
      if (this.briefs[key]) return this.briefs[key]
      this.briefLoading = { ...this.briefLoading, [key]: true }
      try {
        const { data } = await api.get('/api/library/story-brief', {
          params: { kind: item.kind, url: item.url, date: item.publication_date },
        })
        if (data.ok) {
          this.briefs = { ...this.briefs, [key]: data.brief }
          return data.brief
        }
        return null
      } finally {
        this.briefLoading = { ...this.briefLoading, [key]: false }
      }
    },
    async refreshCatalog() {
      this.refreshing = true
      try {
        await api.post('/api/library/refresh')
        // The sync runs in the background; give it a moment then reload.
        await new Promise((r) => setTimeout(r, 4000))
        await this.fetchOverview()
      } finally {
        this.refreshing = false
      }
    },
  },
})
