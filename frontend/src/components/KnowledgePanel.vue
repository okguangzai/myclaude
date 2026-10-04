<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  Upload,
  FileText,
  Search,
  Database,
  ArrowUpRight,
  LoaderCircle,
} from "lucide-vue-next";
import { api, json } from "../api";
const emit = defineEmits(["changed"]);
const documents = ref<any[]>([]),
  query = ref(""),
  result = ref<any>(null),
  busy = ref(""),
  error = ref(""),
  success = ref(""),
  picker = ref<HTMLInputElement>();
async function refresh() {
  documents.value = await api("/documents");
}
async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  busy.value = "upload";
  error.value = "";
  success.value = "";
  try {
    const data = new FormData();
    data.append("file", file);
    const r = await api("/documents", { method: "POST", body: data });
    success.value = `已入库 ${r.name} · ${r.chunks} 个片段 · ${r.dimension} 维`;
    await refresh();
    emit("changed");
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = "";
    if (picker.value) picker.value.value = "";
  }
}
async function search() {
  if (!query.value.trim()) return;
  busy.value = "query";
  error.value = "";
  try {
    result.value = await api(
      "/retrieve",
      json("POST", { query: query.value, top_k: 5 }),
    );
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = "";
  }
}
onMounted(() => refresh().catch((e) => (error.value = e.message)));
</script>
<template>
  <div class="page-intro">
    <div>
      <span class="eyebrow">CONNECTED KNOWLEDGE</span>
      <h2>让知识成为可调用的能力。</h2>
      <p>上传资料、检验召回，再交给 Agent 组织答案。</p>
    </div>
    <span class="badge"
      ><Database :size="13" />{{ documents.length }} 份文档</span
    >
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="success" class="notice">{{ success }}</div>
  <div class="knowledge-grid">
    <section>
      <button class="upload-zone" :disabled="!!busy" @click="picker?.click()">
        <div class="upload-icon">
          <LoaderCircle
            v-if="busy === 'upload'"
            class="spin"
            :size="23"
          /><Upload v-else :size="23" />
        </div>
        <h3>
          {{
            busy === "upload" ? "正在切块、向量化与入库…" : "添加你的学习资料"
          }}
        </h3>
        <p>点击选择 Markdown、文本或代码文件</p>
        <span>UTF-8 · 单文件 ≤ 2 MB · 内容将发送到向量模型</span></button
      ><input
        ref="picker"
        type="file"
        accept=".md,.txt,.py,.rs,.js,.ts,.vue,.html,.css"
        hidden
        @change="upload"
      />
      <div class="panel document-list">
        <div class="section-head">
          <h3>已索引文档</h3>
          <span class="small muted"
            >{{ documents.reduce((n, d) => n + d.chunks, 0) }} 个片段</span
          >
        </div>
        <div v-for="d in documents" :key="d.id" class="document-row">
          <div class="file-icon"><FileText :size="19" /></div>
          <div>
            <strong>{{ d.name }}</strong>
            <p>{{ d.chunks }} 块 · {{ d.dimension }} 维 · {{ d.model }}</p>
          </div>
          <span class="badge">{{ d.backend }}</span>
        </div>
        <div v-if="!documents.length" class="empty-small">
          还没有文档。上传第一份资料，建立你的知识库。
        </div>
      </div>
    </section>
    <section class="panel retrieval-panel">
      <div class="section-head">
        <h3>检索实验室</h3>
        <ArrowUpRight :size="18" />
      </div>
      <p class="muted small">
        直接观察召回的片段和相似度，理解 Agent 看到了什么。
      </p>
      <form @submit.prevent="search">
        <textarea
          v-model="query"
          rows="3"
          aria-label="检索问题"
          placeholder="例如：Agent 循环如何执行工具调用？"
        ></textarea
        ><button class="btn primary full" :disabled="!!busy || !query.trim()">
          <LoaderCircle
            v-if="busy === 'query'"
            class="spin"
            :size="16"
          /><Search v-else :size="16" />{{
            busy === "query" ? "检索中…" : "检索知识库"
          }}
        </button>
      </form>
      <div v-if="result">
        <div class="result-summary">
          {{ result.hits.length }} 个相关片段<span class="badge">{{
            result.backend
          }}</span>
        </div>
        <div v-if="result.warning" class="notice">{{ result.warning }}</div>
        <article
          v-for="(hit, i) in result.hits"
          :key="hit.id"
          class="retrieval-hit"
        >
          <div>
            <strong
              ><span class="hit-number">{{ i + 1 }}</span
              >{{ hit.name }}</strong
            ><span class="score">{{ hit.score.toFixed(3) }}</span>
          </div>
          <p>{{ hit.text }}</p>
        </article>
      </div>
      <div v-else class="retrieval-empty">
        <Search :size="32" />
        <p>每一个好答案，<br />都从找到合适的资料开始。</p>
      </div>
    </section>
  </div>
</template>
