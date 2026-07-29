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

<script>
export default {
  name: 'Dashboard',
  props: {
    config: { type: Object, default: () => ({}) },
    allowRefresh: { type: Boolean, default: false },
  },
  data: () => ({ summary: null }),
  mounted() { this.fetch(); },
  methods: {
    async fetch() {
      try {
        const resp = await this.$root.$api?.get('plugin/LibraryCleaner/result');
        if (resp?.data) this.summary = resp.data;
      } catch (e) { /* ignore */ }
    },
    formatSize(n) {
      if (!n) return '0 B';
      const u = ['B','KB','MB','GB','TB'];
      let i = 0, s = n;
      while (Math.abs(s) >= 1024 && i < u.length-1) { s /= 1024; i++; }
      return s.toFixed(1) + ' ' + u[i];
    },
  },
};
</script>
