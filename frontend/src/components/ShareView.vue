<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api";
import Markdown from "./Markdown.vue";
const props = defineProps<{ token: string }>(),
  data = ref<any>(),
  error = ref("");
onMounted(async () => {
  try {
    data.value = await api(
      "/features/shares/" + encodeURIComponent(props.token),
    );
  } catch (e: any) {
    error.value = e.message;
  }
});
</script>
<template>
  <main style="max-width: 850px; margin: 40px auto; padding: 24px">
    <a :href="'./'">返回 MyClaude</a>
    <div v-if="error" role="alert" class="notice error">{{ error }}</div>
    <template v-if="data"
      ><h1>{{ data.title }}</h1>
      <p class="muted">
        只读分享快照 ·
        {{ new Date(data.expires_at * 1000).toLocaleString() }} 到期 ·
        需要网站登录
      </p>
      <article
        v-for="(m, i) in data.messages"
        :key="i"
        style="padding: 20px 0; border-bottom: 1px solid #ddd"
      >
        <strong>{{ m.role === "user" ? "用户" : "助手" }}</strong
        ><Markdown :text="m.content" /></article
    ></template>
  </main>
</template>
