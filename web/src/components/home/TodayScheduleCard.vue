<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { nowIST } from '../../composables/useDate'

const props = defineProps({
  sessions: { type: Array, default: () => [] },
})
const router = useRouter()

function isCurrentClass(session) {
  if (!session.start_time || !session.end_time) return false
  const now = nowIST()
  const [sh, sm] = session.start_time.split(':').map(Number)
  const [eh, em] = session.end_time.split(':').map(Number)
  const startMin = sh * 60 + (sm || 0)
  const endMin = eh * 60 + (em || 0)
  const nowMin = now.getHours() * 60 + now.getMinutes()
  return nowMin >= startMin && nowMin < endMin
}

function isPast(session) {
  if (!session.end_time) return false
  const now = nowIST()
  const [eh, em] = session.end_time.split(':').map(Number)
  const endMin = eh * 60 + (em || 0)
  const nowMin = now.getHours() * 60 + now.getMinutes()
  return nowMin >= endMin
}

// Show max 3 classes
const visibleSessions = computed(() => props.sessions.slice(0, 3))
const hasMore = computed(() => props.sessions.length > 3)
</script>

<template>
  <section class="flex flex-col gap-3">
    <div class="flex items-baseline justify-between">
      <h2 class="text-body font-semibold text-ink-2">Today's schedule</h2>
      <button type="button" class="text-caption text-accent tappable rounded" @click="router.push('/schedule')">
        See all →
      </button>
    </div>
    <div class="group-list">
      <div v-if="!sessions.length" class="group-row flex items-center gap-3 text-body text-ink-2">
        <span class="material-symbols-outlined text-[18px] text-ink-3">event_available</span>
        No classes today. Enjoy!
      </div>
      <div
        v-for="(s, i) in visibleSessions"
        :key="s.id || i"
        class="group-row flex items-center gap-4 tappable transition-opacity"
        :class="{ 'opacity-50': isPast(s) || s.cancelled }"
        @click="router.push('/schedule')"
      >
        <span
          class="text-body tabular-nums w-12 shrink-0"
          :class="isCurrentClass(s) && !s.cancelled ? 'text-accent font-semibold' : 'font-medium text-ink'"
        >
          {{ s.start_time }}
        </span>
        <div class="min-w-0 flex-1 flex items-center gap-2">
          <span class="w-1 h-8 rounded-full shrink-0" :class="isCurrentClass(s) && !s.cancelled ? 'bg-accent' : 'bg-hairline'" />
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-body text-ink truncate" :class="{ 'line-through': s.cancelled }">{{ s.course }}</p>
              <span v-if="s.cancelled" class="text-[10px] font-semibold text-ink-3 uppercase px-1.5 py-0.5 bg-fill rounded">Cancelled</span>
              <span v-else-if="s.change === 'added'" class="text-[10px] font-semibold text-accent uppercase px-1.5 py-0.5 bg-accent/10 rounded">Extra</span>
              <span v-else-if="isCurrentClass(s)" class="text-[10px] font-semibold text-accent uppercase px-1.5 py-0.5 bg-accent/10 rounded">Now</span>
            </div>
            <p v-if="s.room" class="text-caption text-ink-3 truncate mt-0.5">
              {{ s.room }}
            </p>
          </div>
        </div>
      </div>
      <div v-if="hasMore" class="group-row text-center py-2">
        <button type="button" class="text-caption text-accent tappable" @click="router.push('/schedule')">
          +{{ sessions.length - 3 }} more classes
        </button>
      </div>
    </div>
  </section>
</template>
