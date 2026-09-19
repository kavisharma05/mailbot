<script setup>
const emit = defineEmits(['pick'])
const suggestions = [
  { icon: 'schedule', label: "What's due this week?" },
  { icon: 'calendar_today', label: "Today's schedule" },
  { icon: 'edit', label: 'Send a mail' },
  { icon: 'help', label: 'Placement updates' },
]
</script>

<template>
  <div class="flex items-center gap-2 overflow-x-auto px-4 pt-2.5 pb-1.5 no-scrollbar max-w-app mx-auto">
    <button
      v-for="s in suggestions"
      :key="s.label"
      type="button"
      class="tappable flex-shrink-0 h-8 px-3 rounded-full border border-hairline bg-card text-caption text-accent flex items-center gap-1.5 hover:bg-accent/5 transition-colors"
      @click="emit('pick', s.label)"
    >
      <span class="material-symbols-outlined text-[14px]">{{ s.icon }}</span>
      {{ s.label }}
    </button>
  </div>
</template>
