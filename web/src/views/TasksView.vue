<script setup>
import { onMounted, ref } from 'vue'
import { useTasksStore } from '../stores/tasks'
import TaskFilters from '../components/tasks/TaskFilters.vue'
import TaskCard from '../components/tasks/TaskCard.vue'
import AddTaskSheet from '../components/tasks/AddTaskSheet.vue'

const tasks = useTasksStore()
const sheetOpen = ref(false)

onMounted(() => tasks.fetchTasks())
</script>

<template>
  <div class="flex flex-col w-full">
    <TaskFilters />
    <div class="px-4 mt-4 pb-32 max-w-app mx-auto w-full">
      <p v-if="tasks.loading" class="text-body text-ink-2 py-8 text-center">Loading…</p>
      <div v-else-if="tasks.tasks.length" class="group-list">
        <TaskCard v-for="t in tasks.tasks" :key="t.id" :task="t" />
      </div>
      <p v-else class="text-body text-ink-2 py-8 text-center">Nothing here. You're caught up.</p>
    </div>
    <button
      type="button"
      class="tappable fixed right-4 bottom-20 z-40 h-12 pl-4 pr-5 rounded-full bg-accent text-white shadow-float flex items-center gap-1.5"
      @click="sheetOpen = true"
    >
      <span class="material-symbols-outlined text-[22px]">add</span>
      <span class="text-body font-semibold">Add</span>
    </button>
    <AddTaskSheet :open="sheetOpen" @close="sheetOpen = false" />
  </div>
</template>
