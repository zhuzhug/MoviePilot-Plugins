<template>
  <v-card variant="flat">
    <v-card-text>
      <AppPage :api="api" :pluginId="pluginId" />
    </v-card-text>
  </v-card>
</template>

<script>
import AppPage from './AppPage.vue';
export default {
  name: 'Page',
  components: { AppPage },
  props: {
    api: { type: Object, required: true },
    pluginId: { type: String, required: true },
  },
};
</script>
