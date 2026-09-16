<script setup>
import { onMounted, ref, nextTick, watch, computed } from 'vue'
import { useChatStore } from '../stores/chat'
import { useToast } from '../composables/useToast'
import api from '../composables/useApi'
import ChatBubble from '../components/chat/ChatBubble.vue'
import EmailDraftCard from '../components/chat/EmailDraftCard.vue'
import QuickActionChips from '../components/chat/QuickActionChips.vue'

const chat = useChatStore()
const { show } = useToast()
const input = ref('')
const thread = ref(null)
const inputEl = ref(null)

const canSend = computed(() => input.value.trim().length > 0 && !chat.loading)

onMounted(() => {
  chat.loadHistory()
  inputEl.value?.focus()
})

watch(
  () => chat.messages.length,
  async () => {
    await nextTick()
    thread.value?.scrollTo({ top: thread.value.scrollHeight, behavior: 'smooth' })
  }
)

function send() {
  if (!canSend.value) return
  const text = input.value.trim()
  input.value = ''
  chat.sendMessage(text)
}

function onPick(label) {
  input.value = label
  inputEl.value?.focus()
}

async function clearChat() {
  try {
    await api.post('/api/chat/reset')
    chat.messages = []
    chat.loadHistory()
    show('Chat cleared')
  } catch {
    show('Could not clear chat')
  }
}
</script>

<template>
  <div class="flex flex-col w-full min-h-0">
    <!-- Chat header with clear button -->
    <div class="sticky top-14 z-30 glass border-b border-hairline">
      <div class="h-11 px-4 flex items-center justify-between max-w-app mx-auto">
        <h1 class="text-title text-ink">Chat</h1>
        <button
          v-if="chat.messages.length > 1"
          type="button"
          class="tappable text-caption text-accent flex items-center gap-1"
          @click="clearChat"
        >
          <span class="material-symbols-outlined text-[16px]">refresh</span>
          Clear
        </button>
      </div>
    </div>

    <div ref="thread" class="flex flex-col gap-2 px-4 pt-4 pb-44 max-w-app mx-auto w-full flex-1 overflow-y-auto">
      <!-- Empty state hint -->
      <div v-if="chat.messages.length <= 1 && !chat.loading" class="flex flex-col items-center justify-center py-8 text-center animate-fade-in">
        <div class="w-16 h-16 rounded-full bg-accent/10 flex items-center justify-center mb-4">
          <span class="material-symbols-outlined text-[28px] text-accent">assistant</span>
        </div>
        <p class="text-body text-ink-2 mb-1">Ask me anything</p>
        <p class="text-caption text-ink-3 max-w-[260px]">
          Deadlines, schedule, draft emails, or just chat about your coursework.
        </p>
      </div>

      <template v-for="(msg, i) in chat.messages" :key="i">
        <ChatBubble :message="msg" />
        <EmailDraftCard v-if="msg.draft" :draft="msg.draft" />
      </template>
      
      <!-- Typing indicator -->
      <div v-if="chat.loading" class="self-start px-4 py-3 rounded-card bg-bubble-in flex gap-1.5 animate-fade-in" aria-label="Mailbot is typing">
        <span class="w-2 h-2 rounded-full bg-ink-3/60 animate-bounce" style="animation-delay: 0ms" />
        <span class="w-2 h-2 rounded-full bg-ink-3/60 animate-bounce" style="animation-delay: 150ms" />
        <span class="w-2 h-2 rounded-full bg-ink-3/60 animate-bounce" style="animation-delay: 300ms" />
      </div>
    </div>

    <!-- Input area -->
    <div class="fixed bottom-[52px] left-0 right-0 z-40 glass border-t border-hairline pb-safe">
      <QuickActionChips @pick="onPick" />
      <div class="px-4 pb-3 pt-1.5 max-w-app mx-auto">
        <div class="flex items-center gap-2">
          <input
            ref="inputEl"
            v-model="input"
            class="flex-1 h-11 px-4 rounded-full bg-card border border-hairline text-body text-ink placeholder:text-ink-3 focus:outline-none focus:border-accent/40 focus:ring-2 focus:ring-accent/25 transition-all duration-200"
            placeholder="Message Mailbot…"
            aria-label="Message Mailbot"
            :disabled="chat.loading"
            @keydown.enter.prevent="send"
          />
          <button
            type="button"
            aria-label="Send message"
            :disabled="!canSend"
            class="tappable w-11 h-11 rounded-full flex items-center justify-center shrink-0 transition-all duration-200"
            :class="canSend ? 'bg-accent text-white' : 'bg-fill text-ink-3'"
            @click="send"
          >
            <span class="material-symbols-outlined text-[20px]">arrow_upward</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
