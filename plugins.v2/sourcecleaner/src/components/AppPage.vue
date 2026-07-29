<template>
  <v-app>
    <v-app-bar flat density="compact">
      <v-app-bar-title>源文件清理</v-app-bar-title>
      <template v-slot:append>
        <v-chip v-if="scanning" color="info" variant="tonal" size="small" class="mr-2">
          <v-progress-circular indeterminate size="16" width="2" class="mr-1" />
          扫描中...
        </v-chip>
        <v-chip v-else-if="summary" color="primary" variant="tonal" size="small" class="mr-2">
          共 {{ summary.total }} 项 · {{ formatSize(summary.total_size) }}
        </v-chip>
        <v-btn v-if="scanning" color="error" variant="tonal" size="small" @click="cancelScan">
          <v-icon start>mdi-close-circle</v-icon>取消
        </v-btn>
        <v-btn v-else color="primary" variant="flat" size="small" @click="startScan" :loading="scanning">
          <v-icon start>mdi-refresh</v-icon>扫描
        </v-btn>
      </template>
    </v-app-bar>

    <v-main>
      <v-container fluid>
        <!-- 扫描中状态 -->
        <v-card v-if="scanning" variant="outlined" class="mb-4">
          <v-card-text class="text-center py-8">
            <v-progress-circular indeterminate color="primary" size="64" class="mb-4" />
            <div class="text-h6 mb-2">正在扫描文件系统...</div>
            <div class="text-caption text-medium-emphasis">扫描过程中请勿关闭页面</div>
            <v-progress-linear indeterminate color="primary" class="mt-4" />
          </v-card-text>
        </v-card>

        <!-- 扫描结果 -->
        <template v-else-if="summary && summary.total > 0">
          <!-- 概览卡片 -->
          <v-row class="mb-4">
            <v-col v-for="cat in categories" :key="cat.id" cols="6" md="3">
              <v-card variant="outlined" :color="getSafetyColor(cat.safety)" class="cursor-pointer" @click="selectedTab = cat.id">
                <v-card-text class="text-center py-3">
                  <v-icon :color="getSafetyColor(cat.safety)" size="28" class="mb-1">{{ cat.icon }}</v-icon>
                  <div class="text-body-2 font-weight-bold">{{ cat.title }}</div>
                  <v-chip :color="getSafetyColor(cat.safety)" variant="flat" size="small" class="mt-1">
                    {{ summary.counts[cat.id] || 0 }}
                  </v-chip>
                </v-card-text>
              </v-card>
            </v-col>
          </v-row>

          <!-- 分类详情 -->
          <v-expansion-panels v-model="expandedPanels">
            <v-expansion-panel v-for="cat in categories" :key="cat.id" :value="cat.id">
              <v-expansion-panel-title>
                <v-icon :color="getSafetyColor(cat.safety)" class="mr-2">{{ cat.icon }}</v-icon>
                {{ cat.title }}
                <v-spacer />
                <v-chip :color="getSafetyColor(cat.safety)" variant="tonal" size="x-small" class="mr-2" prepend-icon="mdi-shield">
                  {{ cat.safetyLabel }}
                </v-chip>
                <v-chip color="warning" variant="flat" size="small">
                  {{ summary.counts[cat.id] || 0 }} · {{ formatSize(getCategorySize(cat.id)) }}
                </v-chip>
              </v-expansion-panel-title>
              <v-expansion-panel-text>
                <v-alert :type="getSafetyType(cat.safety)" variant="tonal" density="compact" class="mb-3">
                  <strong>{{ cat.safetyLabel }}</strong>：{{ cat.safetyDesc }}
                </v-alert>
                <v-list density="compact" lines="two">
                  <v-list-item v-for="(item, idx) in getItems(cat.id)" :key="idx">
                    <v-list-item-title style="word-break: break-all; font-family: monospace; font-size: 0.875rem;">
                      {{ item.path }}
                    </v-list-item-title>
                    <v-list-item-subtitle>
                      <span v-if="item.size">大小: {{ formatSize(item.size) }}</span>
                      <span v-if="item.target"> · {{ item.target }}</span>
                    </v-list-item-subtitle>
                    <template v-slot:append v-if="allowDelete">
                      <v-btn icon="mdi-delete" color="error" variant="text" size="small" @click="deleteItem(item.path, cat.id)" />
                    </template>
                  </v-list-item>
                </v-list>
                <v-alert v-if="summary.truncated[cat.id]" type="info" variant="tonal" density="compact" class="mt-2">
                  已达到展示上限（{{ maxDisplay }}），如需查看更多请调整设置
                </v-alert>
              </v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>
        </template>

        <!-- 空状态 -->
        <v-card v-else variant="outlined">
          <v-card-text class="text-center py-12">
            <v-icon size="64" color="grey-lighten-1" class="mb-4">mdi-magnify-close</v-icon>
            <div class="text-h6 text-grey">暂无扫描结果</div>
            <div class="text-caption text-medium-emphasis mt-2">点击右上角"扫描"按钮开始检测</div>
          </v-card-text>
        </v-card>
      </v-container>
    </v-main>
  </v-app>
</template>

<script>
export default {
  name: 'AppPage',
  props: {
    api: { type: Object, required: true },
    pluginId: { type: String, required: true },
    navKey: { type: String, default: 'main' },
  },
  data: () => ({
    scanning: false,
    summary: null,
    items: {},
    expandedPanels: [],
    selectedTab: null,
    maxDisplay: 200,
    allowDelete: false,
    categories: [
      { id: 'dangling', title: '悬空软链', icon: 'mdi-link-variant-off', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '软链目标已不存在，删除链接无任何影响' },
      { id: 'orphan_meta', title: '孤儿元数据', icon: 'mdi-file-document-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '无对应视频的 .nfo/.jpg/.srt，不影响播放' },
      { id: 'empty_dir', title: '空目录', icon: 'mdi-folder-open-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '空目录可直接删除' },
      { id: 'dup_resource', title: '重复资源', icon: 'mdi-content-duplicate', safety: 'warn', safetyLabel: '需确认', safetyDesc: '同片不同版本，删除前确认保留哪个' },
      { id: 'source_transferred', title: '已入库源文件', icon: 'mdi-file-check', safety: 'danger', safetyLabel: '谨慎', safetyDesc: '下载目录中已整理到媒体库的文件' },
      { id: 'source_orphan', title: '孤立源文件', icon: 'mdi-file-question', safety: 'danger', safetyLabel: '谨慎', safetyDesc: '无下载任务跟踪，可能被其他工具使用' },
      { id: 'source_torrent', title: '无效种子文件', icon: 'mdi-file-remove-outline', safety: 'warn', safetyLabel: '需确认', safetyDesc: '.torrent 文件残留' },
      { id: 'source_empty_dir', title: '源目录空目录', icon: 'mdi-folder-open-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '源数据空目录' },
      { id: 'source_dup', title: '源文件重复', icon: 'mdi-file-multiple', safety: 'warn', safetyLabel: '需确认', safetyDesc: '下载目录中的重复文件' },
    ],
  }),
  mounted() {
    this.fetchResult();
  },
  methods: {
    async fetchResult() {
      try {
        const resp = await this.api.get(`plugin/${this.pluginId}/result`);
        if (resp.data) {
          this.summary = resp.data;
          this.items = resp.data.items || {};
          this.maxDisplay = resp.data.max_display || 200;
          this.allowDelete = resp.data.allow_delete || false;
        }
      } catch (e) {
        console.error('获取结果失败', e);
      }
    },
    async startScan() {
      this.scanning = true;
      try {
        await this.api.get(`plugin/${this.pluginId}/scan`);
        await this.fetchResult();
      } catch (e) {
        console.error('扫描失败', e);
      } finally {
        this.scanning = false;
      }
    },
    async cancelScan() {
      try {
        await this.api.get(`plugin/${this.pluginId}/cancel`);
      } catch (e) {
        console.error('取消失败', e);
      }
    },
    async deleteItem(path, category) {
      if (!confirm(`确认删除？\n${path}`)) return;
      try {
        await this.api.post(`plugin/${this.pluginId}/delete_item`, { path, category });
        await this.fetchResult();
      } catch (e) {
        alert('删除失败：' + (e.message || e));
      }
    },
    getItems(categoryId) {
      return (this.items[categoryId] || []).slice(0, this.maxDisplay);
    },
    getCategorySize(categoryId) {
      return (this.items[categoryId] || []).reduce((sum, it) => sum + (it.size || 0), 0);
    },
    formatSize(n) {
      if (!n || n === 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      let size = n;
      while (Math.abs(size) >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return size.toFixed(1) + ' ' + units[i];
    },
    getSafetyColor(safety) {
      return { safe: 'success', warn: 'warning', danger: 'error' }[safety] || 'grey';
    },
    getSafetyType(safety) {
      return { safe: 'success', warn: 'warning', danger: 'error' }[safety] || 'info';
    },
  },
};
</script>
