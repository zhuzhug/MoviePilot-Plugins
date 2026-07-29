<template>
  <v-card variant="flat">
    <v-card-title>源文件清理设置</v-card-title>
    <v-card-text>
      <v-switch v-model="config.enabled" label="启用插件" color="primary" />
      <v-textarea v-model="config.scan_dirs" label="扫描目录（每行一个）" rows="2" variant="outlined" density="compact" />
      <v-select v-model="config.scan_scope" :items="[{title:'仅媒体库',value:'media_only'},{title:'仅源数据',value:'source_only'},{title:'全部',value:'all'}]" label="扫描范围" variant="outlined" density="compact" />
      <v-switch v-model="config.allow_delete" label="允许删除" color="error" />
    </v-card-text>
    <v-card-actions>
      <v-spacer />
      <v-btn color="primary" @click="emit('save', config)">保存</v-btn>
    </v-card-actions>
  </v-card>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['save', 'close'])
const config = ref({ ...props.initialConfig })
</script>
