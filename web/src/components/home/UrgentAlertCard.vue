<script setup>
import { ref, computed } from 'vue'
import { useTasksStore } from '../../stores/tasks'
import { useToast } from '../../composables/useToast'
import { shortDeadline } from '../../composables/useDate'
import { displayTitle, displayDetail } from '../../composables/useTaskDisplay'

const props = defineProps({
  task: { type: Object, default: null },
})
const tasks = useTasksStore()
const { show } = useToast()
const acting = ref(false)

const isOverdue = computed(() => props.task?.status_filter === 'missed')
const heading = computed(() => displayTitle(props.task))
const subtitle = computed(() => displayDetail(props.task))
const deadlineText = computed(() => {
  if (!props.task) return ''
  const text = shortDeadline(props.task.deadline_display)
  if (isOverdue.value) return `Overdue · ${text}`
  return text || 'Due soon'
})

async function markDone() {
  if (!props.task?.id || acting.value) return
  acting.value = true
  try {
    await tasks.markDone(props.task.id)
    show('Done ✓')
  } finally {
    acting.value = false
  }
}

async function snooze() {
  if (!props.task?.id || acting.value) return
  acting.value = true
  try {
    await tasks.snooze(props.task.id, 2)
    show('Snoozed 2h')
  } finally {
    acting.value = false
  }
}
</script>

<template>
  <section v-if="task" class="card p-4" :class="{ 'opacity-60': acting }">
    <div class="flex flex-col gap-4">
      <!-- Header with deadline badge -->
      <div class="flex items-center justify-between">
        <span
          class="text-caption font-semibold px-2.5 py-1 rounded-full"
          :class="isOverdue ? 'bg-danger/10 text-danger' : 'bg-accent/10 text-accent'"
        >
          {{ deadlineText }}
        </span>
        <span v-if="task.category && task.category !== 'other'" class="text-caption text-ink-3 capitalize">
          {{ task.category }}
        </span>
      </div>
      
      <!-- Task content -->
      <div>
        <h2 class="text-title text-ink leading-snug">{{ heading }}</h2>
        <p v-if="subtitle" class="text-body text-ink-2 mt-1 leading-relaxed line-clamp-2">
          {{ subtitle }}
        </p>
      </div>
      
      <!-- Actions -->
      <div class="flex items-center gap-3">
        <button type="button" class="btn-primary flex-1" :disabled="acting" @click="markDone">
          <span class="material-symbols-outlined text-[18px]">check</span>
          Done
        </button>
        <button type="button" class="btn-secondary flex-1" :disabled="acting" @click="snooze">
          Snooze
        </button>
      </div>
    </div>
  </section>

  <!-- Empty state when no urgent task -->
  <section v-else class="card p-5 text-center">
    <div class="flex flex-col items-center gap-2">
      <div class="w-12 h-12 rounded-full bg-success/10 flex items-center justify-center">
        <span class="material-symbols-outlined text-[24px] text-success">check_circle</span>
      </div>
      <p class="text-body text-ink-2">All caught up!</p>
      <p class="text-caption text-ink-3">No urgent deadlines</p>
    </div>
  </section>
</template>
