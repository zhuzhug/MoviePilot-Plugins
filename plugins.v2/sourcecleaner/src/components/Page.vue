<template>
  <v-card variant="flat">
    <v-card-text>
      <AppPage :api="api" :pluginId="pluginId" />
    </v-card-text>
  </v-card>
</template>

<script setup>
import AppPage from './AppPage.vue'
defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, required: true },
})
</script>
