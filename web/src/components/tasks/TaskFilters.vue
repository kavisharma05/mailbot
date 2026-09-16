<script setup>
import { ref, computed } from 'vue'
import { useTasksStore } from '../../stores/tasks'

const tasks = useTasksStore()
const searchValue = ref('')

const segments = computed(() => [
  { id: 'all', label: 'All', count: tasks.counts.all },
  { id: 'due-soon', label: 'Due', count: tasks.counts['due-soon'] },
  { id: 'missed', label: 'Overdue', count: tasks.counts.missed },
  { id: 'undated', label: 'No date', count: tasks.counts.undated },
])

function setStatus(id) {
  tasks.statusFilter = id
  tasks.fetchTasks()
}

let searchTimer = null
function onSearch(e) {
  const value = e.target.value
  searchValue.value = value
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    tasks.search = value
    tasks.fetchTasks()
  }, 300)
}

function clearSearch() {
  searchValue.value = ''
  tasks.search = ''
  tasks.fetchTasks()
}
</script>

<template>
  <div class="flex flex-col gap-4 px-4 pt-6 max-w-app mx-auto w-full">
    <header class="flex items-baseline justify-between">
      <h1 class="text-ltitle text-ink">Tasks</h1>
      <span class="text-caption text-ink-3">{{ tasks.activeCount }} open</span>
    </header>

    <div class="relative">
      <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-[18px] text-ink-3 pointer-events-none">
        search
      </span>
      <input
        :value="searchValue"
        class="input w-full pl-9 pr-9"
        type="search"
        placeholder="Search tasks…"
        aria-label="Search tasks"
        @input="onSearch"
      />
      <button
        v-if="searchValue"
        type="button"
        aria-label="Clear search"
        class="tappable absolute right-2 top-1/2 -translate-y-1/2 w-6 h-6 rounded-full bg-fill flex items-center justify-center text-ink-3 hover:text-ink transition-colors"
        @click="clearSearch"
      >
        <span class="material-symbols-outlined text-[14px]">close</span>
      </button>
    </div>

    <div class="bg-fill rounded-full p-0.5 flex" role="tablist" aria-label="Filter tasks">
      <button
        v-for="seg in segments"
        :key="seg.id"
        type="button"
        role="tab"
        :aria-selected="tasks.statusFilter === seg.id"
        class="tappable flex-1 h-8 rounded-full text-caption font-medium transition-all duration-200 ease-apple flex items-center justify-center gap-1"
        :class="tasks.statusFilter === seg.id ? 'bg-card text-ink shadow-seg' : 'text-ink-2'"
        @click="setStatus(seg.id)"
      >
        {{ seg.label }}
        <span
          v-if="seg.count > 0 && seg.id !== 'all'"
          class="text-[10px] px-1 py-px rounded-full"
          :class="seg.id === 'missed' && seg.count > 0 ? 'bg-danger/15 text-danger' : 'bg-fill-2 text-ink-3'"
        >
          {{ seg.count }}
        </span>
      </button>
    </div>
  </div>
</template>
