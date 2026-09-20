<script setup>
import { onMounted, ref, computed } from 'vue'
import { useLibraryStore } from '../stores/library'

const library = useLibraryStore()
const tab = ref('today')
const search = ref('')
const searching = ref(false)

const KIND_META = {
  epw: { label: 'EPW', icon: 'menu_book', color: 'text-purple-600' },
  ft: { label: 'Financial Times', icon: 'newspaper', color: 'text-pink-600' },
  et_prime: { label: 'ET Prime', icon: 'trending_up', color: 'text-amber-600' },
}

const groupedPublications = computed(() => {
  const groups = {}
  for (const item of library.publications) {
    const key = `${item.kind}|${item.publication_date}`
    if (!groups[key]) {
      groups[key] = { kind: item.kind, date: item.publication_date, items: [] }
    }
    groups[key].items.push(item)
  }
  return Object.values(groups)
})

onMounted(async () => {
  await library.fetchOverview()
  library.fetchPublications()
})

async function onSearch() {
  searching.value = true
  try {
    await library.fetchResources(search.value)
  } finally {
    searching.value = false
  }
}

function switchTab(next) {
  tab.value = next
  if (next === 'resources' && !library.resources.length) library.fetchResources()
  if (next === 'analysis' && !library.analysis) library.fetchAnalysis()
  if (next === 'today' && !library.publications.length) library.fetchPublications()
}

function kindMeta(kind) {
  return KIND_META[kind] || { label: kind, icon: 'article', color: 'text-ink-2' }
}
</script>

<template>
  <div class="flex flex-col w-full pb-28">
    <div class="px-4 mt-4 max-w-app mx-auto w-full">
      <!-- Header stats -->
      <div class="card p-4 mb-4">
        <div class="flex items-center gap-3">
          <div class="w-11 h-11 rounded-2xl bg-accent-soft flex items-center justify-center">
            <span class="material-symbols-outlined text-[24px] text-accent">local_library</span>
          </div>
          <div class="flex-1 min-w-0">
            <h1 class="text-title">Library e-resources</h1>
            <p class="text-caption text-ink-3 truncate">
              <template v-if="library.overview?.configured">
                {{ library.resourceCount }} resources
                <span v-if="library.lastDailyRun"> · digest {{ library.lastDailyRun }}</span>
              </template>
              <template v-else>Portal credentials not configured</template>
            </p>
          </div>
          <button
            type="button"
            class="btn-tertiary !px-3"
            :disabled="library.refreshing"
            @click="library.refreshCatalog()"
          >
            <span class="material-symbols-outlined text-[18px]" :class="library.refreshing ? 'animate-spin' : ''">sync</span>
          </button>
        </div>
      </div>

      <!-- Tabs -->
      <div class="flex gap-2 mb-4">
        <button
          v-for="t in [
            { id: 'today', label: 'Today' },
            { id: 'resources', label: 'Resources' },
            { id: 'analysis', label: 'Analysis' },
          ]"
          :key="t.id"
          type="button"
          class="flex-1 h-9 rounded-full text-[13px] font-medium transition-colors"
          :class="tab === t.id ? 'bg-accent text-white' : 'bg-fill text-ink-2'"
          @click="switchTab(t.id)"
        >
          {{ t.label }}
        </button>
      </div>

      <!-- Loading -->
      <div v-if="library.loading" class="group-list animate-pulse">
        <div v-for="i in 3" :key="i" class="card p-4 mb-2">
          <div class="skeleton h-5 w-2/3"></div>
          <div class="skeleton h-3 w-1/3 mt-2"></div>
        </div>
      </div>

      <!-- Today: latest publications -->
      <div v-else-if="tab === 'today'">
        <div v-if="groupedPublications.length" class="flex flex-col gap-3">
          <div v-for="group in groupedPublications" :key="group.kind + group.date" class="card p-4">
            <div class="flex items-center gap-2 mb-2">
              <span class="material-symbols-outlined text-[20px]" :class="kindMeta(group.kind).color">
                {{ kindMeta(group.kind).icon }}
              </span>
              <span class="text-body font-semibold">{{ kindMeta(group.kind).label }}</span>
              <span class="text-caption text-ink-3 ml-auto">{{ group.date }}</span>
            </div>
            <div class="flex flex-col divide-y divide-hairline">
              <a
                v-for="item in group.items"
                :key="item.id || item.url"
                :href="item.url"
                target="_blank"
                rel="noopener"
                class="py-2 flex flex-col gap-0.5 tappable"
              >
                <span class="text-body text-ink leading-snug">{{ item.title }}</span>
                <span v-if="item.section" class="text-caption text-ink-3">{{ item.section }}</span>
              </a>
            </div>
          </div>
        </div>
        <div v-else class="flex flex-col items-center justify-center py-12 text-center">
          <div class="w-16 h-16 rounded-full bg-fill flex items-center justify-center mb-4">
            <span class="material-symbols-outlined text-[28px] text-ink-3">menu_book</span>
          </div>
          <p class="text-body text-ink-2">No publications yet</p>
          <p class="text-caption text-ink-3 mt-1">
            The daily briefing lands here after the 7:15 AM run
          </p>
        </div>
      </div>

      <!-- Resources catalog -->
      <div v-else-if="tab === 'resources'">
        <div class="relative mb-3">
          <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-[20px] text-ink-3">search</span>
          <input
            v-model="search"
            type="search"
            placeholder="Search resources…"
            class="w-full h-11 rounded-full bg-fill pl-10 pr-4 text-body outline-none focus:ring-2 ring-accent"
            @input="onSearch"
          />
        </div>
        <div v-if="library.resources.length" class="flex flex-col gap-3">
          <div v-for="cat in library.resources" :key="cat.name" class="card p-4">
            <p class="text-caption font-semibold text-ink-3 uppercase tracking-wide mb-2">
              {{ cat.name }} · {{ cat.resources.length }}
            </p>
            <div class="flex flex-col divide-y divide-hairline">
              <a
                v-for="res in cat.resources"
                :key="res.portal_id"
                :href="res.url"
                target="_blank"
                rel="noopener"
                class="py-2 flex items-center gap-2 tappable"
              >
                <span class="text-body text-ink flex-1 leading-snug">{{ res.name }}</span>
                <span class="material-symbols-outlined text-[16px] text-ink-3">open_in_new</span>
              </a>
            </div>
          </div>
        </div>
        <div v-else class="flex flex-col items-center justify-center py-12 text-center">
          <div class="w-16 h-16 rounded-full bg-fill flex items-center justify-center mb-4">
            <span class="material-symbols-outlined text-[28px] text-ink-3">cloud_off</span>
          </div>
          <p class="text-body text-ink-2">Catalog not synced yet</p>
          <p class="text-caption text-ink-3 mt-1">Tap the sync button above to fetch it</p>
        </div>
      </div>

      <!-- Analysis report -->
      <div v-else-if="tab === 'analysis'">
        <div v-if="library.analysis?.available" class="card p-4">
          <p class="text-caption text-ink-3 mb-2">
            Updated {{ new Date(library.analysis.created_at).toLocaleString() }}
          </p>
          <div class="text-body text-ink whitespace-pre-wrap leading-relaxed">{{ library.analysis.content }}</div>
        </div>
        <div v-else class="flex flex-col items-center justify-center py-12 text-center">
          <div class="w-16 h-16 rounded-full bg-fill flex items-center justify-center mb-4">
            <span class="material-symbols-outlined text-[28px] text-ink-3">analytics</span>
          </div>
          <p class="text-body text-ink-2">No analysis yet</p>
          <p class="text-caption text-ink-3 mt-1">Run <code>python -m imt_access analyze</code> to generate one</p>
        </div>
      </div>
    </div>
  </div>
</template>
