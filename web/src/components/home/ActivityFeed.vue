<script setup>
import { computed } from 'vue'
import { nowIST } from '../../composables/useDate'

const props = defineProps({
  items: { type: Array, default: () => [] },
})

function timeAgo(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = nowIST()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)
  
  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

function categoryIcon(cat) {
  const map = {
    deadline: 'assignment',
    placement: 'work',
    admin: 'folder',
    other: 'mail',
  }
  return map[cat] || 'notifications'
}

const visibleItems = computed(() => props.items.slice(0, 5))
</script>

<template>
  <section v-if="items.length" class="flex flex-col gap-2">
    <h2 class="text-body font-semibold text-ink-2 px-1">Recent activity</h2>
    <div class="group-list">
      <div v-for="(item, i) in visibleItems" :key="i" class="group-row flex items-start gap-3">
        <div class="w-8 h-8 rounded-full bg-fill flex items-center justify-center shrink-0 mt-0.5">
          <span class="material-symbols-outlined text-[16px] text-ink-2">{{ categoryIcon(item.category) }}</span>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-body text-ink leading-snug line-clamp-2">{{ item.title }}</p>
          <p class="text-caption text-ink-3 mt-0.5 flex items-center gap-1.5">
            <span v-if="item.category" class="capitalize">{{ item.category }}</span>
            <span v-if="item.sender">· {{ item.sender }}</span>
            <span v-if="item.created_at" class="ml-auto shrink-0">{{ timeAgo(item.created_at) }}</span>
          </p>
        </div>
      </div>
    </div>
  </section>
</template>
