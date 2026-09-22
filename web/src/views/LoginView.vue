<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const step = ref(1)
const username = ref('')
const code = ref('')
const message = ref('')
const inviteCode = ref('')
const loading = ref(false)

async function requestCode() {
  message.value = ''
  loading.value = true
  try {
    const data = await auth.requestCode(username.value.trim().replace(/^@/, ''))
    if (!data.ok) {
      message.value = data.message || 'Could not send code.'
      if (data.needs_bot_start && data.invite_code) {
        inviteCode.value = data.invite_code
      }
      return
    }
    step.value = 2
    message.value = data.message
  } catch (e) {
    message.value = e.response?.data?.detail || 'Request failed.'
  } finally {
    loading.value = false
  }
}

async function verify() {
  message.value = ''
  loading.value = true
  try {
    await auth.verify(username.value.trim().replace(/^@/, ''), code.value.trim())
    router.replace('/')
  } catch (e) {
    message.value = e.response?.data?.detail || 'Invalid code.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="max-w-md mx-auto px-4 py-8">
    <h1 class="text-2xl font-semibold text-ink mb-2">Sign in</h1>
    <p class="text-sm text-ink-muted mb-6">Use the Telegram username on your invite. We send a one-time code in the bot. After you sign in, connect Gmail on the home screen.</p>

    <div v-if="step === 1" class="space-y-4">
      <label class="block text-sm font-medium text-ink">Telegram username</label>
      <input
        v-model="username"
        class="w-full rounded-xl border border-line bg-surface px-4 py-3"
        placeholder="@yourname"
        autocomplete="username"
      />
      <button
        class="w-full rounded-xl bg-brand text-white py-3 font-medium disabled:opacity-50"
        :disabled="loading || !username.trim()"
        @click="requestCode"
      >
        Send code
      </button>
    </div>

    <div v-else class="space-y-4">
      <label class="block text-sm font-medium text-ink">6-digit code</label>
      <input
        v-model="code"
        class="w-full rounded-xl border border-line bg-surface px-4 py-3 tracking-widest"
        placeholder="123456"
        inputmode="numeric"
        maxlength="6"
      />
      <button
        class="w-full rounded-xl bg-brand text-white py-3 font-medium disabled:opacity-50"
        :disabled="loading || code.length < 4"
        @click="verify"
      >
        Sign in
      </button>
      <button class="text-sm text-brand" type="button" @click="step = 1">Use a different username</button>
    </div>

    <p v-if="inviteCode" class="mt-4 text-sm text-ink-muted">
      Open the invite link in Telegram and press Start. The bot sends your code. Then come back here and enter it.
    </p>
    <p v-if="message" class="mt-4 text-sm text-ink-muted">{{ message }}</p>
  </div>
</template>
