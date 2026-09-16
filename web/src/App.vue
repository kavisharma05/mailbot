<script setup>
import { onMounted } from 'vue'
import AppHeader from './components/common/AppHeader.vue'
import BottomNav from './components/common/BottomNav.vue'
import Toast from './components/common/Toast.vue'
import { useWebSocket } from './composables/useWebSocket'
import { useTasksStore } from './stores/tasks'

const { start } = useWebSocket()
const tasks = useTasksStore()

onMounted(() => {
  start()
  tasks.fetchTasks()
})
</script>

<template>
  <AppHeader />
  <main class="w-full pt-14 pb-20 bg-canvas min-h-screen">
    <RouterView />
  </main>
  <BottomNav />
  <Toast />
</template>
