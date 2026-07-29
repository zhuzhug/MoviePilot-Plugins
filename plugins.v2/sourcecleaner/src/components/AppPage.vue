<template>
  <v-app>
    <v-app-bar flat density="compact">
      <v-app-bar-title>源文件清理</v-app-bar-title>
      <template v-slot:append>
        <v-chip v-if="scanning" color="info" variant="tonal" size="small" class="mr-2">
          <v-progress-circular indeterminate size="16" width="1" class="mr-1" />
          扫描中...
        </v-chip>
        <v-chip v-else-if="summary" color="primary" variant="tonal" size="small" class="mr-2">
          共 {{ summary.total }} 项 · {{ fmtSize(summary.total_size) }}
        </v-chip>
        <v-btn v-if="scanning" color="error" variant="tonal" size="small" @click="cancelScan">
          <v-icon start size="small">mdi-close-circle</v-icon>取消
        </v-btn>
        <v-btn v-else color="primary" variant="flat" size="small" @click="startScan">
          <v-icon start size="small">mdi-refresh</v-icon>扫描
        </v-btn>
      </template>
    </v-app-bar>

    <v-main>
      <v-container fluid>
        <v-card v-if="scanning" variant="outlined" class="mb-4">
          <v-card-text class="text-center py-8">
            <v-progress-circular indeterminate color="primary" size="64" class="mb-4" />
            <div class="text-h6 mb-2">正在扫描文件系统...</div>
            <div class="text-caption text-medium-emphasis">请稍候</div>
            <v-progress-linear indeterminate color="primary" class="mt-4" />
          </v-card-text>
        </v-card>

        <template v-else-if="summary && summary.total > 0">
          <v-row class="mb-4">
            <v-col v-for="cat in categories" :key="cat.id" cols="6" md="3">
              <v-card variant="outlined" :color="sColor(cat.s)" class="cursor-pointer" @click="expanded = [cat.id]">
                <v-card-text class="text-center py-3">
                  <v-icon :color="sColor(cat.s)" size="28" class="mb-1">{{ cat.icon }}</v-icon>
                  <div class="text-body-2 font-weight-bold">{{ cat.title }}</div>
                  <v-chip :color="sColor(cat.s)" variant="flat" size="small" class="mt-1">{{ summary.counts[cat.id] || 0 }}</v-chip>
                </v-card-text>
              </v-card>
            </v-col>
          </v-row>

          <v-expansion-panels v-model="expanded">
            <v-expansion-panel v-for="cat in categories" :key="cat.id" :value="cat.id">
              <v-expansion-panel-title>
                <v-icon :color="sColor(cat.s)" class="mr-2">{{ cat.icon }}</v-icon>
                {{ cat.title }}
                <v-spacer />
                <v-chip :color="sColor(cat.s)" variant="tonal" size="x-small" class="mr-2">{{ cat.sl }}</v-chip>
                <v-chip color="warning" variant="flat" size="small">{{ summary.counts[cat.id] || 0 }} · {{ fmtSize(catSize(cat.id)) }}</v-chip>
              </v-expansion-panel-title>
              <v-expansion-panel-text>
                <v-alert :type="sColor(cat.s)" variant="tonal" density="compact" class="mb-3">
                  <strong>{{ cat.sl }}</strong>：{{ cat.sd }}
                </v-alert>
                <v-list density="compact" lines="two">
                  <v-list-item v-for="(it, i) in catItems(cat.id)" :key="i">
                    <v-list-item-title style="word-break:break-all;font-family:monospace;font-size:.875rem">{{ it.path }}</v-list-item-title>
                    <v-list-item-subtitle>
                      <span v-if="it.size">{{ fmtSize(it.size) }}</span>
                      <span v-if="it.target"> · {{ it.target }}</span>
                    </v-list-item-subtitle>
                    <template v-slot:append v-if="allowDelete">
                      <v-btn icon="mdi-delete" color="error" variant="text" size="small" @click="delItem(it.path, cat.id)" />
                    </template>
                  </v-list-item>
                </v-list>
                <v-alert v-if="summary.truncated[cat.id]" type="info" variant="tonal" density="compact" class="mt-2">已达到展示上限</v-alert>
              </v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>
        </template>

        <v-card v-else variant="outlined">
          <v-card-text class="text-center py-12">
            <v-icon size="64" color="grey-lighten-1" class="mb-4">mdi-magnify-close</v-icon>
            <div class="text-h6 text-grey">暂无扫描结果</div>
            <div class="text-caption text-medium-emphasis mt-2">点击右上角"扫描"按钮开始</div>
          </v-card-text>
        </v-card>
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup>
import { ref, onMounted } from 'vue'

const props = defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, required: true },
})

const scanning = ref(false)
const summary = ref(null)
const items = ref({})
const expanded = ref([])
const allowDelete = ref(false)
const maxDisplay = ref(200)

const categories = [
  { id: 'dangling', title: '悬空软链', icon: 'mdi-link-variant-off', s: 'success', sl: '安全可删', sd: '目标已不存在' },
  { id: 'orphan_meta', title: '孤儿元数据', icon: 'mdi-file-document-outline', s: 'success', sl: '安全可删', sd: '无对应视频' },
  { id: 'empty_dir', title: '空目录', icon: 'mdi-folder-open-outline', s: 'success', sl: '安全可删', sd: '空目录' },
  { id: 'dup_resource', title: '重复资源', icon: 'mdi-content-duplicate', s: 'warning', sl: '需确认', sd: '同片不同版本' },
  { id: 'source_transferred', title: '已入库源文件', icon: 'mdi-file-check', s: 'error', sl: '谨慎', sd: '已整理到库' },
  { id: 'source_orphan', title: '孤立源文件', icon: 'mdi-file-question', s: 'error', sl: '谨慎', sd: '无下载器跟踪' },
  { id: 'source_torrent', title: '无效种子文件', icon: 'mdi-file-remove-outline', s: 'warning', sl: '需确认', sd: '.torrent 残留' },
  { id: 'source_empty_dir', title: '源目录空目录', icon: 'mdi-folder-open-outline', s: 'success', sl: '安全可删', sd: '源数据空目录' },
  { id: 'source_dup', title: '源文件重复', icon: 'mdi-file-multiple', s: 'warning', sl: '需确认', sd: '下载目录重复' },
]

function unwrap(r) {
  if (r && r.data !== undefined && (r.success !== undefined || r.code !== undefined)) return r.data
  return r?.data ?? r
}

onMounted(() => fetchResult())

async function fetchResult() {
  try {
    const resp = await props.api.get(`plugin/${props.pluginId}/result`)
    const d = unwrap(resp)
    if (d) {
      summary.value = d
      items.value = d.items || {}
      maxDisplay.value = d.max_display || 200
      allowDelete.value = d.allow_delete || false
    }
  } catch (e) { console.error('fetchResult error:', e) }
}

async function startScan() {
  scanning.value = true
  try {
    const resp = await props.api.get(`plugin/${props.pluginId}/scan`)
    const d = unwrap(resp)
    if (d) {
      summary.value = d
      items.value = d.items || {}
    }
  } catch (e) { console.error('scan error:', e) }
  finally { scanning.value = false }
}

async function cancelScan() {
  try { await props.api.get(`plugin/${props.pluginId}/cancel`) } catch (e) {}
}

async function delItem(path, category) {
  if (!confirm(`确认删除？\n${path}`)) return
  try {
    await props.api.post(`plugin/${props.pluginId}/delete_item`, { path, category })
    await fetchResult()
  } catch (e) { alert('删除失败') }
}

function catItems(id) { return (items.value[id] || []).slice(0, maxDisplay.value) }
function catSize(id) { return (items.value[id] || []).reduce((s, it) => s + (it.size || 0), 0) }
function fmtSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, s = n
  while (Math.abs(s) >= 1024 && i < u.length - 1) { s /= 1024; i++ }
  return s.toFixed(1) + ' ' + u[i]
}
function sColor(s) { return { success: 'success', warning: 'warning', error: 'error' }[s] || 'grey' }
</script>
