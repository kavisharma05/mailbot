<script setup>
import { computed } from 'vue'
import { nowIST, todayIST } from '../../composables/useDate'

const props = defineProps({
  session: { type: Object, required: true },
  highlight: { type: Boolean, default: false },
  isToday: { type: Boolean, default: false },
})

// Check if current class (only for today)
const isCurrentClass = computed(() => {
  if (!props.isToday || !props.session.start_time || !props.session.end_time) return false
  const now = nowIST()
  const today = todayIST()
  const [sh, sm] = props.session.start_time.split(':').map(Number)
  const [eh, em] = props.session.end_time.split(':').map(Number)
  const startMin = sh * 60 + (sm || 0)
  const endMin = eh * 60 + (em || 0)
  const nowMin = now.getHours() * 60 + now.getMinutes()
  return nowMin >= startMin && nowMin < endMin
})

const isPast = computed(() => {
  if (!props.isToday || !props.session.end_time) return false
  const now = nowIST()
  const [eh, em] = props.session.end_time.split(':').map(Number)
  const endMin = eh * 60 + (em || 0)
  const nowMin = now.getHours() * 60 + now.getMinutes()
  return nowMin >= endMin
})
</script>

<template>
  <div
    class="group-row flex gap-4 transition-opacity duration-200"
    :class="{ 'opacity-50': isPast || session.cancelled }"
  >
    <div class="w-20 shrink-0 pt-0.5 text-right">
      <p class="text-body font-semibold tabular-nums" :class="isCurrentClass && !session.cancelled ? 'text-accent' : 'text-ink'">
        {{ session.start_time }}
      </p>
      <p class="text-caption text-ink-3 tabular-nums mt-0.5">{{ session.end_time }}</p>
    </div>
    <div class="relative min-w-0 flex-1 border-l-2 pl-3" :class="session.cancelled ? 'border-hairline' : isCurrentClass ? 'border-accent' : highlight ? 'border-accent/50' : 'border-hairline'">
      <span v-if="isCurrentClass && !session.cancelled" class="absolute -left-[5px] top-1.5 w-2 h-2 rounded-full bg-accent animate-pulse-soft" />
      <div class="flex items-start justify-between gap-2">
        <p class="text-body font-medium text-ink leading-snug" :class="{ 'line-through': session.cancelled }">{{ session.course }}</p>
        <span v-if="session.cancelled" class="text-[10px] font-semibold text-ink-3 uppercase tracking-wide shrink-0">Cancelled</span>
        <span v-else-if="session.change === 'added'" class="text-[10px] font-semibold text-accent uppercase tracking-wide shrink-0">Extra</span>
        <span v-else-if="isCurrentClass" class="text-[10px] font-semibold text-accent uppercase tracking-wide shrink-0">Now</span>
      </div>
      <p v-if="session.room || session.faculty" class="text-caption text-ink-3 mt-1 flex items-center gap-1.5">
        <span v-if="session.room" class="flex items-center gap-0.5">
          <span class="material-symbols-outlined text-[12px]">location_on</span>
          {{ session.room }}
        </span>
        <span v-if="session.room && session.faculty">·</span>
        <span v-if="session.faculty">{{ session.faculty }}</span>
      </p>
      <p v-if="session.notes" class="text-caption text-ink-2 mt-1.5 leading-relaxed">{{ session.notes }}</p>
    </div>
  </div>
</template>
