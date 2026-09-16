<script setup>
import { ref } from 'vue'
import { useTasksStore } from '../../stores/tasks'
import { useToast } from '../../composables/useToast'
import { shortDeadline } from '../../composables/useDate'
import { displayTitle, displayDetail } from '../../composables/useTaskDisplay'

const props = defineProps({
  task: { type: Object, required: true },
})
const tasks = useTasksStore()
const { show } = useToast()
const acting = ref(false)
const showActions = ref(false)

async function toggleDone() {
  if (acting.value) return
  acting.value = true
  try {
    if (props.task.status === 'done') {
      await tasks.reopen(props.task.id)
      show('Task reopened')
    } else {
      await tasks.markDone(props.task.id)
      show('Done ✓')
    }
  } finally {
    acting.value = false
  }
}

async function snooze() {
  if (acting.value) return
  acting.value = true
  showActions.value = false
  try {
    await tasks.snooze(props.task.id, 2)
    show('Snoozed 2h')
  } finally {
    acting.value = false
  }
}

function categoryIcon(cat) {
  const map = {
    deadline: 'assignment',
    placement: 'work',
    admin: 'folder',
    certification: 'workspace_premium',
    other: 'label',
  }
  return map[cat] || 'label'
}
</script>

<template>
  <div
    class="group-row flex items-start gap-3 relative"
    :class="{ 'opacity-60': acting }"
  >
    <button
      type="button"
      :aria-label="task.status === 'done' ? 'Reopen task' : 'Mark task done'"
      :aria-pressed="task.status === 'done'"
      :disabled="acting"
      class="tappable w-6 h-6 mt-0.5 rounded-full shrink-0 flex items-center justify-center border transition-all duration-200"
      :class="task.status === 'done' ? 'bg-accent border-accent text-white' : 'border-ink-3/50 text-transparent hover:border-accent/50'"
      @click="toggleDone"
    >
      <span class="material-symbols-outlined text-[16px]">check</span>
    </button>
    <div class="min-w-0 flex-1">
      <p
        class="text-body leading-snug transition-colors duration-200"
        :class="task.status === 'done' ? 'text-ink-3 line-through' : 'text-ink'"
      >
        {{ displayTitle(task) }}
      </p>
      <p v-if="displayDetail(task)" class="text-caption text-ink-2 mt-0.5 line-clamp-1">{{ displayDetail(task) }}</p>
      <p class="text-caption mt-1 flex items-center gap-1.5 flex-wrap">
        <span
          v-if="task.category && task.category !== 'other'"
          class="inline-flex items-center gap-0.5 text-ink-3"
        >
          <span class="material-symbols-outlined text-[12px]">{{ categoryIcon(task.category) }}</span>
          <span class="capitalize">{{ task.category }}</span>
          <span class="mx-0.5">·</span>
        </span>
        <span
          v-if="task.deadline_display"
          :class="task.status_filter === 'missed' ? 'text-danger font-medium' : task.status_filter === 'due-soon' && task.status !== 'done' ? 'text-accent' : 'text-ink-3'"
        >
          {{ shortDeadline(task.deadline_display) }}
        </span>
        <span v-else-if="task.status !== 'done'" class="text-ink-3">No due date</span>
        <span v-if="task.sender" class="text-ink-3 truncate">· {{ task.sender }}</span>
      </p>
    </div>
    <!-- Snooze button for non-done tasks -->
    <button
      v-if="task.status !== 'done' && task.deadline_display"
      type="button"
      aria-label="Snooze 2 hours"
      :disabled="acting"
      class="tappable w-8 h-8 rounded-full shrink-0 flex items-center justify-center text-ink-3 hover:bg-fill hover:text-accent transition-all duration-200"
      @click="snooze"
    >
      <span class="material-symbols-outlined text-[18px]">snooze</span>
    </button>
  </div>
</template>
