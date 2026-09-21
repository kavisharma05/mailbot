<script setup>
import { wsConnected } from '../../composables/useWebSocket'
import { useAuthStore } from '../../stores/auth'
import { useRouter } from 'vue-router'

const auth = useAuthStore()
const router = useRouter()

async function logout() {
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <header class="fixed top-0 w-full z-50 pt-safe glass border-b border-hairline">
    <div class="h-14 px-4 flex items-center justify-between max-w-app mx-auto">
      <span class="text-title text-ink">Mailbot</span>
      <div class="flex items-center gap-3">
        <button
          v-if="auth.isLoggedIn"
          type="button"
          class="text-caption text-ink-3 tappable"
          @click="logout"
        >
          Log out
        </button>
        <div class="flex items-center gap-1.5">
        <span
          class="w-1.5 h-1.5 rounded-full transition-colors duration-200"
          :class="wsConnected ? 'bg-success' : 'bg-ink-3'"
        />
        <span class="text-caption text-ink-3">{{ wsConnected ? 'Live' : 'Offline' }}</span>
        </div>
      </div>
    </div>
  </header>
</template>
