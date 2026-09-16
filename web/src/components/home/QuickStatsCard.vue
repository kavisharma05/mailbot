<script setup>
import { useRouter } from 'vue-router'

defineProps({
  stats: { type: Object, required: true },
})

const router = useRouter()

const items = [
  { key: 'deadlines_due', label: 'Due this week', icon: 'assignment_late', color: 'text-danger', route: '/tasks' },
  { key: 'classes_today', label: 'Classes today', icon: 'school', color: 'text-accent', route: '/schedule' },
  { key: 'placement', label: 'Placement', icon: 'work', color: 'text-success', route: '/tasks' },
]
</script>

<template>
  <section aria-label="Overview" class="card grid grid-cols-3 divide-x divide-hairline overflow-hidden">
    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      class="tappable px-3 py-4 flex flex-col items-center gap-1.5 hover:bg-fill/50 transition-colors"
      @click="router.push(item.route)"
    >
      <div class="flex items-center gap-1.5">
        <span class="material-symbols-outlined text-[18px]" :class="item.color">{{ item.icon }}</span>
        <span class="text-[24px] leading-[28px] font-semibold text-ink tabular-nums">{{ stats[item.key] ?? 0 }}</span>
      </div>
      <span class="text-[11px] text-ink-3 text-center leading-tight">{{ item.label }}</span>
    </button>
  </section>
</template>
