import { defineStore } from 'pinia'
import api from '../composables/useApi'

function nowLabel() {
  return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [],
    loading: false,
    draft: null,
    indexedHint: 'Gmail connected',
  }),
  actions: {
    async loadHistory() {
      try {
        const { data } = await api.get('/api/chat/history')
        const hist = data.messages || []
        this.messages = hist.map((m) => ({
          role: m.role === 'user' ? 'user' : 'bot',
          text: m.text,
          time: '',
        }))
        if (!this.messages.length) {
          this.messages.push({
            role: 'bot',
            text: 'Hey! I can answer questions about your courses, scan emails for deadlines, or draft faculty emails.',
            time: nowLabel(),
          })
        }
      } catch {
        if (!this.messages.length) {
          this.messages.push({
            role: 'bot',
            text: 'Hey! Ask me about deadlines, classes, or say "send email to … saying …"',
            time: nowLabel(),
          })
        }
      }
    },
    appendUser(text) {
      this.messages.push({ role: 'user', text, time: nowLabel() })
    },
    appendBot(text, draft = null) {
      this.messages.push({ role: 'bot', text, time: nowLabel(), draft })
      if (draft) this.draft = draft
    },
    async sendMessage(text, verified = false) {
      const trimmed = text.trim()
      if (!trimmed) return
      this.appendUser(trimmed)
      this.loading = true
      this.draft = null
      try {
        const { data } = await api.post('/api/chat/message', { message: trimmed, verified })
        this.appendBot(data.reply, data.draft)
      } catch (e) {
        this.appendBot('Sorry, I could not reach the server. Try again in a moment.')
      } finally {
        this.loading = false
      }
    },
    async sendDraft(draft) {
      await api.post('/api/compose/send', {
        to: draft.to,
        subject: draft.subject,
        body: draft.body,
      })
      this.draft = null
    },
  },
})
