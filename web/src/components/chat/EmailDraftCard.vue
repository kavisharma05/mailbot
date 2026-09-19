<script setup>
import { computed, ref } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useToast } from '../../composables/useToast'

const props = defineProps({
  draft: { type: Object, required: true },
})

const toLine = computed(() => {
  const name = (props.draft.to_display || '').trim()
  const email = (props.draft.to || '').trim()
  if (name && email && name.toLowerCase() !== email.toLowerCase()) {
    return `${name} · ${email}`
  }
  return name || email
})
const expanded = ref(false)
const sending = ref(false)
const sent = ref(false)
const chat = useChatStore()
const { show } = useToast()

async function send() {
  if (sending.value || sent.value) return
  sending.value = true
  try {
    await chat.sendDraft({
      to: props.draft.to,
      subject: props.draft.subject,
      body: props.draft.body,
    })
    sent.value = true
    show('Email sent')
  } catch {
    show('Could not send. Try again.')
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="card self-start w-full max-w-[92%] overflow-hidden">
    <div class="px-4 pt-4 pb-3 flex flex-col gap-2 border-b border-hairline">
      <div class="flex items-baseline gap-3 text-body">
        <span class="text-caption text-ink-3 w-14 shrink-0">To</span>
        <span class="text-ink truncate">{{ toLine }}</span>
      </div>
      <div class="flex items-baseline gap-3 text-body">
        <span class="text-caption text-ink-3 w-14 shrink-0">Subject</span>
        <span class="text-ink font-medium">{{ draft.subject }}</span>
      </div>
    </div>
    <div class="px-4 py-3">
      <p class="text-body text-ink-2 whitespace-pre-wrap" :class="expanded ? '' : 'line-clamp-4'">{{ draft.body }}</p>
      <button
        v-if="draft.body?.length > 240"
        type="button"
        class="text-caption text-accent mt-1.5 tappable rounded"
        @click="expanded = !expanded"
      >
        {{ expanded ? 'Show less' : 'Read more' }}
      </button>
    </div>
    <div class="px-4 pb-4">
      <button
        type="button"
        class="w-full h-11 rounded-card text-body font-semibold flex items-center justify-center gap-2 tappable"
        :class="sent ? 'bg-fill text-ink-2' : 'bg-accent text-white'"
        :disabled="sending || sent"
        @click="send"
      >
        {{ sent ? 'Sent' : sending ? 'Sending…' : 'Send' }}
      </button>
    </div>
  </div>
</template>
