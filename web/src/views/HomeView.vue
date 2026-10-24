<script setup>
import { onMounted, computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useDashboardStore } from '../stores/dashboard'
import { nowIST, shortDeadline } from '../composables/useDate'
import { displayTitle, displayDetail } from '../composables/useTaskDisplay'
import QuickStatsCard from '../components/home/QuickStatsCard.vue'
import UrgentAlertCard from '../components/home/UrgentAlertCard.vue'
import TodayScheduleCard from '../components/home/TodayScheduleCard.vue'
import ActivityFeed from '../components/home/ActivityFeed.vue'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const dashboard = useDashboardStore()
const router = useRouter()
const activityItems = computed(() => dashboard.data?.activity?.items || [])
const refreshing = ref(false)

onMounted(() => dashboard.fetchDashboard())

const todayLabel = computed(() => nowIST().toLocaleDateString('en-IN', {
  weekday: 'long',
  month: 'long',
  day: 'numeric',
}))

function greeting() {
  const h = nowIST().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}

async function refresh() {
  refreshing.value = true
  try {
    await dashboard.fetchDashboard()
  } finally {
    refreshing.value = false
  }
}

async function connectGmail() {
  try {
    const url = await auth.gmailConnectUrl()
    window.location.href = url
  } catch {
    /* dashboard may show error */
  }
}
</script>

<template>
  <div class="flex flex-col gap-6 px-4 pt-6 pb-28 max-w-app mx-auto w-full">
    <!-- Header with greeting -->
    <header class="flex flex-col gap-1">
      <div class="flex items-center justify-between">
        <span class="text-caption text-ink-3">{{ todayLabel }}</span>
        <button
          type="button"
          class="tappable w-8 h-8 rounded-full flex items-center justify-center text-ink-3 hover:bg-fill transition-colors"
          :class="{ 'animate-spin': refreshing }"
          aria-label="Refresh"
          @click="refresh"
        >
          <span class="material-symbols-outlined text-[18px]">refresh</span>
        </button>
      </div>
      <h1 class="text-ltitle text-ink">{{ greeting() }}, {{ dashboard.greetingName }}</h1>
    </header>

    <div
      v-if="dashboard.error"
      class="card px-4 py-3 flex items-center justify-between gap-3"
    >
      <p class="text-caption text-ink-2">{{ dashboard.error }}</p>
      <button type="button" class="text-caption text-accent tappable shrink-0" @click="refresh">Retry</button>
    </div>

    <div
      v-if="dashboard.data && dashboard.data.gmail_connected === false"
      class="card px-4 py-4 flex flex-col gap-2 border border-amber-200/80 bg-amber-50/40"
    >
      <p class="text-body text-ink font-medium">
        {{ dashboard.data.gmail_needs_reconnect ? 'Reconnect Gmail' : 'Connect Gmail' }}
      </p>
      <p class="text-caption text-ink-2">
        {{
          dashboard.data.gmail_needs_reconnect
            ? 'Mail watching is paused until you sign in with Google again (same as Telegram /status).'
            : 'Link your inbox to track deadlines and send mail from the app.'
        }}
      </p>
      <button type="button" class="btn-primary mt-1" @click="connectGmail">
        {{ dashboard.data.gmail_needs_reconnect ? 'Reconnect Gmail' : 'Connect Gmail' }}
      </button>
    </div>

    <!-- Loading skeleton -->
    <template v-if="dashboard.loading && !dashboard.data">
      <div class="card p-4 flex flex-col gap-3 animate-pulse">
        <div class="skeleton h-4 w-24"></div>
        <div class="skeleton h-6 w-full"></div>
        <div class="skeleton h-4 w-32"></div>
      </div>
      <div class="card p-4 flex gap-4 animate-pulse">
        <div class="skeleton h-12 flex-1"></div>
        <div class="skeleton h-12 flex-1"></div>
        <div class="skeleton h-12 flex-1"></div>
      </div>
    </template>

    <template v-else>
      <UrgentAlertCard :task="dashboard.urgentTask" />

      <QuickStatsCard :stats="dashboard.stats" />

      <TodayScheduleCard :sessions="dashboard.todayClasses" />

      <!-- Upcoming tasks -->
      <section v-if="dashboard.upcoming.length" class="flex flex-col gap-3">
        <div class="flex items-baseline justify-between">
          <h2 class="text-body font-semibold text-ink-2">Upcoming</h2>
          <button type="button" class="text-caption text-accent tappable rounded" @click="router.push('/tasks')">
            See all →
          </button>
        </div>
        <div class="group-list">
          <div
            v-for="item in dashboard.upcoming.slice(0, 4)"
            :key="item.id"
            class="group-row tappable"
            @click="router.push('/tasks')"
          >
            <div class="flex items-start justify-between gap-4">
              <p class="text-body text-ink leading-snug flex-1 min-w-0">{{ displayTitle(item) }}</p>
              <span
                class="text-caption shrink-0 font-medium text-right"
                :class="item.status_filter === 'missed' ? 'text-danger' : item.status_filter === 'due-soon' ? 'text-accent' : 'text-ink-3'"
              >
                {{ shortDeadline(item.deadline_display) }}
              </span>
            </div>
            <p class="text-caption mt-1 text-ink-3 line-clamp-1">
              <span v-if="displayDetail(item)">{{ displayDetail(item) }}</span>
              <template v-else>
                <span v-if="item.category && item.category !== 'other'" class="capitalize">{{ item.category }}</span>
                <span v-if="item.category && item.sender"> · </span>
                <span v-if="item.sender">{{ item.sender }}</span>
              </template>
            </p>
          </div>
        </div>
      </section>

      <ActivityFeed :items="activityItems" />
    </template>

    <!-- FAB for chat -->
    <button
      type="button"
      aria-label="Ask Mailbot"
      class="tappable fixed right-4 bottom-20 z-40 w-14 h-14 rounded-full bg-accent text-white shadow-float flex items-center justify-center hover:scale-105 transition-transform"
      @click="router.push('/chat')"
    >
      <span class="material-symbols-outlined text-[26px]">chat_bubble</span>
    </button>
  </div>
</template>
