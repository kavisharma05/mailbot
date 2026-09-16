<script setup>
import { computed } from 'vue'
import { useToast } from '../../composables/useToast'

const { message, visible } = useToast()

// Detect toast type from message content
const toastType = computed(() => {
  const msg = message.value?.toLowerCase() || ''
  if (msg.includes('✓') || msg.includes('done') || msg.includes('saved') || msg.includes('sent') || msg.includes('added')) return 'success'
  if (msg.includes('fail') || msg.includes('error') || msg.includes('could not')) return 'error'
  return 'info'
})

const icon = computed(() => {
  switch (toastType.value) {
    case 'success': return 'check_circle'
    case 'error': return 'error'
    default: return 'info'
  }
})
</script>

<template>
  <Transition name="toast">
    <div
      v-if="visible"
      role="status"
      aria-live="polite"
      class="fixed top-20 left-1/2 -translate-x-1/2 z-[60] px-4 py-2.5 rounded-2xl bg-ink/95 text-white text-caption font-medium shadow-float flex items-center gap-2 backdrop-blur-sm"
    >
      <span class="material-symbols-outlined text-[16px]" :class="toastType === 'success' ? 'text-success' : toastType === 'error' ? 'text-danger' : 'text-white/80'">
        {{ icon }}
      </span>
      {{ message }}
    </div>
  </Transition>
</template>

<style scoped>
.toast-enter-active {
  transition: all 0.25s cubic-bezier(0.25, 0.1, 0.25, 1);
}
.toast-leave-active {
  transition: all 0.2s ease-in;
}
.toast-enter-from {
  opacity: 0;
  transform: translate(-50%, -12px) scale(0.95);
}
.toast-leave-to {
  opacity: 0;
  transform: translate(-50%, -8px);
}
</style>
