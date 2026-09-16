<script setup>
import { ref, watch, computed } from 'vue'
import { useTasksStore } from '../../stores/tasks'
import { useToast } from '../../composables/useToast'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close'])
const tasks = useTasksStore()
const { show } = useToast()
const task = ref('')
const deadline = ref('')
const category = ref('deadline')
const saving = ref(false)

const canSave = computed(() => task.value.trim().length > 0 && !saving.value)

// Reset form when sheet closes
watch(() => props.open, (isOpen) => {
  if (!isOpen) {
    setTimeout(() => {
      task.value = ''
      deadline.value = ''
      category.value = 'deadline'
    }, 200)
  }
})

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    await tasks.createTask({
      task: task.value.trim(),
      deadline_text: deadline.value || undefined,
      category: category.value,
    })
    show('Task added')
    emit('close')
  } catch {
    show('Failed to save')
  } finally {
    saving.value = false
  }
}

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && canSave.value) {
    e.preventDefault()
    save()
  }
  if (e.key === 'Escape') {
    emit('close')
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="sheet">
      <div
        v-if="open"
        class="fixed inset-0 z-50 bg-black/30 flex flex-col justify-end"
        @click.self="emit('close')"
        @keydown="handleKeydown"
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="New task"
          class="w-full max-w-app mx-auto bg-card rounded-t-card p-6 flex flex-col gap-5 shadow-float animate-slide-up"
        >
          <div class="w-9 h-1 bg-ink-3/30 rounded-full self-center cursor-grab" @click="emit('close')" />
          <h2 class="text-title text-ink">New task</h2>
          <div class="flex flex-col gap-1">
            <input
              v-model="task"
              class="input w-full"
              placeholder="What needs to get done?"
              autofocus
              @keydown.enter.prevent="save"
            />
          </div>
          <div class="flex flex-col gap-1">
            <div class="relative">
              <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-[18px] text-ink-3 pointer-events-none">
                schedule
              </span>
              <input
                v-model="deadline"
                class="input w-full pl-10"
                placeholder="Due — e.g. Friday 5pm, tomorrow"
              />
            </div>
            <span class="text-[11px] text-ink-3 pl-1">Natural language like "next Monday" works</span>
          </div>
          <div class="flex flex-col gap-2">
            <span class="text-caption text-ink-2 pl-1">Category</span>
            <div class="bg-fill rounded-full p-0.5 flex" role="radiogroup" aria-label="Category">
              <button
                v-for="c in ['deadline', 'placement', 'admin', 'other']"
                :key="c"
                type="button"
                role="radio"
                :aria-checked="category === c"
                class="tappable flex-1 h-8 rounded-full text-caption font-medium capitalize transition-all duration-200"
                :class="category === c ? 'bg-card text-ink shadow-seg' : 'text-ink-2'"
                @click="category = c"
              >
                {{ c === 'deadline' ? 'Academic' : c }}
              </button>
            </div>
          </div>
          <div class="flex gap-3 pt-2">
            <button type="button" class="btn-secondary flex-1" @click="emit('close')">Cancel</button>
            <button
              type="button"
              class="btn-primary flex-1"
              :disabled="!canSave"
              :class="{ 'opacity-50 cursor-not-allowed': !canSave }"
              @click="save"
            >
              <span v-if="saving" class="flex items-center gap-2">
                <span class="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></span>
                Saving
              </span>
              <span v-else>Add task</span>
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
