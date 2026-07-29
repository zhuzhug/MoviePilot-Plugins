<template>
  <v-card variant="outlined">
    <v-card-title class="text-subtitle-1">源文件清理</v-card-title>
    <v-card-text class="text-center py-4">
      <div v-if="summary" class="text-h5 font-weight-bold text-primary">{{ summary.total }}</div>
      <div v-if="summary" class="text-caption text-medium-emphasis">项残留 · {{ formatSize(summary.total_size) }}</div>
      <div v-else class="text-caption text-grey">暂无数据</div>
    </v-card-text>
  </v-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'

const props = defineProps({
  config: { type: Object, default: () => ({}) },
  allowRefresh: { type: Boolean, default: false },
})

const summary = ref(null)

onMounted(async () => {
  try {
    const resp = await fetch('/api/v1/plugin/SourceCleaner/result?token=' + (window.__token || ''))
    const data = await resp.json()
    if (data.data) summary.value = data.data
  } catch (e) { /* ignore */ }
})

function formatSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, s = n
  while (Math.abs(s) >= 1024 && i < u.length - 1) { s /= 1024; i++ }
  return s.toFixed(1) + ' ' + u[i]
}
</script>
