<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTasksStore } from '../../stores/tasks'

const route = useRoute()
const router = useRouter()
const tasks = useTasksStore()
const badge = computed(() => tasks.dueSoonBadge)

const tabs = [
  { name: 'home', path: '/', icon: 'home', label: 'Home' },
  { name: 'tasks', path: '/tasks', icon: 'check_circle', label: 'Tasks', badge: true },
  { name: 'chat', path: '/chat', icon: 'chat_bubble', label: 'Chat' },
  { name: 'schedule', path: '/schedule', icon: 'calendar_month', label: 'Schedule' },
]
</script>

<template>
  <nav class="fixed bottom-0 w-full z-50 pb-safe glass border-t border-hairline">
    <div class="flex items-stretch h-[52px] max-w-app mx-auto">
      <button
        v-for="tab in tabs"
        :key="tab.name"
        type="button"
        class="tappable flex-1 flex flex-col items-center justify-center gap-0.5 rounded-none"
        :class="route.name === tab.name ? 'text-accent' : 'text-ink-3'"
        :aria-current="route.name === tab.name ? 'page' : undefined"
        @click="router.push(tab.path)"
      >
        <span class="relative">
          <span class="material-symbols-outlined text-[24px]">{{ tab.icon }}</span>
          <span
            v-if="tab.badge && badge > 0"
            class="absolute -top-1 -right-2 min-w-[15px] h-[15px] px-1 rounded-full bg-danger text-white text-[10px] font-semibold flex items-center justify-center"
          >
            {{ badge }}
          </span>
        </span>
        <span class="text-[10px] leading-[12px] font-medium">{{ tab.label }}</span>
      </button>
    </div>
  </nav>
</template>
