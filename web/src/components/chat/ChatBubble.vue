<script setup>
defineProps({
  message: { type: Object, required: true },
})
</script>

<template>
  <div
    class="max-w-[78%] px-4 py-2.5 rounded-card text-body leading-relaxed whitespace-pre-wrap"
    :class="
      message.role === 'user'
        ? 'self-end bg-accent text-white'
        : 'self-start bg-bubble-in text-ink'
    "
  >
    {{ message.text }}
  </div>
</template>
