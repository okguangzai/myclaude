<script setup lang="ts">
import { ref, onMounted } from "vue";
import { ArrowUpRight, X, RefreshCw } from "lucide-vue-next";
import { api } from "../api";
const runs = ref<any[]>([]),
  detail = ref<any>(null),
  error = ref("");
const labels: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
  cancelled: "已停止",
  running: "运行中",
  timeout: "超时",
  limit: "达到限制",
  interrupted: "服务中断",
};
async function refresh() {
  try {
    runs.value = await api("/runs");
  } catch (e: any) {
    error.value = e.message;
  }
}
async function open(id: string) {
  try {
    detail.value = await api("/runs/" + id);
  } catch (e: any) {
    error.value = e.message;
  }
}
function time(s: string) {
  return new Date(s).toLocaleString("zh-CN", { hour12: false });
}
onMounted(refresh);
</script>
<template>
  <div class="page-intro">
    <div>
      <span class="eyebrow">OBSERVABILITY</span>
      <h2>每一步，都有迹可循。</h2>
      <p>查看模型轮次、工具结果与终止原因，理解 Agent 的执行过程。</p>
    </div>
    <button class="btn" @click="refresh">
      <RefreshCw :size="15" />刷新记录
    </button>
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <section class="panel">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务 / 会话</th>
            <th>状态</th>
            <th>模型</th>
            <th>轮次</th>
            <th>Tokens</th>
            <th>开始时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in runs"
            :key="r.id"
            @click="open(r.id)"
            tabindex="0"
            @keydown.enter="open(r.id)"
          >
            <td class="run-title">
              {{ r.title || "新对话" }}<small>{{ r.id.slice(0, 8) }}</small>
            </td>
            <td>
              <span class="status" :class="r.status">{{
                labels[r.status] || r.status
              }}</span>
            </td>
            <td>{{ r.model }}</td>
            <td>{{ r.turns }}</td>
            <td>{{ (r.input_tokens + r.output_tokens).toLocaleString() }}</td>
            <td class="muted small">{{ time(r.started_at) }}</td>
            <td><ArrowUpRight :size="16" /></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!runs.length" class="empty-small">
        运行记录还为空。完成一次对话后，这里会显示完整执行轨迹。
      </div>
    </div>
  </section>
  <div v-if="detail" class="drawer-backdrop" @click.self="detail = null">
    <section class="run-drawer">
      <div class="section-head">
        <div>
          <span class="eyebrow">RUN DETAIL</span>
          <h3>执行轨迹</h3>
        </div>
        <button class="icon-btn" aria-label="关闭详情" @click="detail = null">
          <X :size="20" />
        </button>
      </div>
      <p class="small muted">{{ detail.id }}</p>
      <div class="run-meta">
        <span class="status" :class="detail.status">{{
          labels[detail.status]
        }}</span
        ><span>{{ detail.model }}</span
        ><span>{{ detail.turns }} 轮</span><span>{{ detail.permission }}</span>
      </div>
      <div v-if="detail.error" class="notice error">{{ detail.error }}</div>
      <div class="event-list">
        <details
          v-for="e in detail.events"
          :key="e.id"
          class="event-item"
          :open="e.kind === 'tool_result' || e.kind === 'run_end'"
        >
          <summary>
            <span class="dot"></span><strong>{{ e.kind }}</strong
            ><span>{{ e.data.name || "" }}</span
            ><time>{{ new Date(e.at).toLocaleTimeString() }}</time>
          </summary>
          <pre>{{ JSON.stringify(e.data, null, 2) }}</pre>
        </details>
      </div>
    </section>
  </div>
</template>
