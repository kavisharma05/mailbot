<script setup>
import { computed } from 'vue'
import { todayIST } from '../../composables/useDate'

const props = defineProps({
  date: { type: String, required: true },
})
const emit = defineEmits(['change'])

const todayIso = todayIST()

const monthLabel = computed(() => {
  const d = new Date(props.date + 'T12:00:00')
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
})

const dayLabel = computed(() => {
  const d = new Date(props.date + 'T12:00:00')
  return d.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
})

const weekDays = computed(() => {
  const anchor = new Date(props.date + 'T12:00:00')
  const start = new Date(anchor)
  start.setDate(anchor.getDate() - ((anchor.getDay() + 6) % 7))
  const days = []
  for (let i = 0; i < 7; i++) {
    const d = new Date(start)
    d.setDate(start.getDate() + i)
    const iso = d.toISOString().slice(0, 10)
    days.push({
      iso,
      letter: d.toLocaleDateString(undefined, { weekday: 'narrow' }),
      num: d.getDate(),
      active: iso === props.date,
      isToday: iso === todayIso,
    })
  }
  return days
})

function shift(delta) {
  const d = new Date(props.date + 'T12:00:00')
  d.setDate(d.getDate() + delta)
  emit('change', d.toISOString().slice(0, 10))
}
</script>

<template>
  <div class="px-4 pt-6 max-w-app mx-auto w-full flex flex-col gap-4">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-ltitle text-ink">{{ monthLabel }}</h1>
        <p class="text-caption text-ink-3 mt-1">{{ dayLabel }}</p>
      </div>
      <div class="flex items-center gap-1">
        <!-- Today button (when not viewing today) -->
        <button
          v-if="date !== todayIso"
          type="button"
          class="tappable h-8 px-3 rounded-full text-caption font-medium text-accent bg-accent/10 mr-1"
          @click="emit('change', todayIso)"
        >
          Today
        </button>
        <button
          type="button"
          aria-label="Previous week"
          class="tappable w-10 h-10 rounded-full text-ink-2 flex items-center justify-center hover:bg-fill transition-colors"
          @click="shift(-7)"
        >
          <span class="material-symbols-outlined text-[22px]">chevron_left</span>
        </button>
        <button
          type="button"
          aria-label="Next week"
          class="tappable w-10 h-10 rounded-full text-ink-2 flex items-center justify-center hover:bg-fill transition-colors"
          @click="shift(7)"
        >
          <span class="material-symbols-outlined text-[22px]">chevron_right</span>
        </button>
      </div>
    </header>

    <div class="flex justify-between" role="tablist" aria-label="Day of week">
      <button
        v-for="day in weekDays"
        :key="day.iso"
        type="button"
        role="tab"
        :aria-selected="day.active"
        class="tappable flex flex-col items-center gap-1 py-1.5 flex-1 rounded-card transition-colors"
        @click="emit('change', day.iso)"
      >
        <span class="text-caption" :class="day.active ? 'text-accent' : 'text-ink-3'">{{ day.letter }}</span>
        <span
          class="w-9 h-9 rounded-full flex items-center justify-center text-body tabular-nums transition-all duration-200"
          :class="
            day.active
              ? 'bg-accent text-white font-semibold shadow-seg'
              : day.isToday
                ? 'text-accent font-semibold ring-2 ring-accent/30'
                : 'text-ink hover:bg-fill'
          "
        >
          {{ day.num }}
        </span>
      </button>
    </div>
  </div>
</template>
