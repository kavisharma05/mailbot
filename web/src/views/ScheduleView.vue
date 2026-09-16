<script setup>
import { onMounted, ref } from 'vue'
import { useScheduleStore } from '../stores/schedule'
import { todayIST } from '../composables/useDate'
import DateCarousel from '../components/schedule/DateCarousel.vue'
import ClassCard from '../components/schedule/ClassCard.vue'

const schedule = useScheduleStore()
const refreshing = ref(false)

onMounted(() => schedule.fetchDay(schedule.date))

function onDateChange(iso) {
  schedule.fetchDay(iso)
}

async function refresh() {
  refreshing.value = true
  try {
    await schedule.forceRefresh()
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <div class="flex flex-col w-full pb-28">
    <DateCarousel :date="schedule.date" @change="onDateChange" />

    <div class="px-4 mt-4 max-w-app mx-auto w-full">
      <!-- Loading skeleton -->
      <div v-if="schedule.loading" class="group-list animate-pulse">
        <div v-for="i in 3" :key="i" class="group-row flex gap-4">
          <div class="w-20 shrink-0">
            <div class="skeleton h-5 w-12"></div>
            <div class="skeleton h-3 w-10 mt-1"></div>
          </div>
          <div class="flex-1 border-l-2 border-hairline pl-3">
            <div class="skeleton h-5 w-3/4"></div>
            <div class="skeleton h-3 w-1/2 mt-1"></div>
          </div>
        </div>
      </div>
      
      <!-- Class list -->
      <div v-else-if="schedule.sessions.length" class="group-list">
        <ClassCard
          v-for="(s, i) in schedule.sessions"
          :key="s.id || i"
          :session="s"
          :highlight="i === 0"
          :is-today="schedule.date === todayIST()"
        />
      </div>
      
      <!-- Empty state -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-center">
        <div class="w-16 h-16 rounded-full bg-fill flex items-center justify-center mb-4">
          <span class="material-symbols-outlined text-[28px] text-ink-3">event_busy</span>
        </div>
        <p class="text-body text-ink-2">No classes</p>
        <p class="text-caption text-ink-3 mt-1">{{ schedule.date === todayIST() ? 'Enjoy your day off!' : 'Nothing scheduled for this day' }}</p>
      </div>

      <div class="flex justify-center mt-6">
        <button type="button" class="btn-tertiary" :disabled="refreshing" @click="refresh">
          <span class="material-symbols-outlined text-[18px]" :class="refreshing ? 'animate-spin' : ''">sync</span>
          {{ refreshing ? 'Syncing…' : 'Sync from Gmail' }}
        </button>
      </div>
    </div>
  </div>
</template>
