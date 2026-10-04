<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from "vue";
import {
  Plus,
  ArrowUp,
  Square,
  Sparkles,
  FileCode2,
  BookOpen,
  ShieldCheck,
  ChevronRight,
  LoaderCircle,
} from "lucide-vue-next";
import { api, json, streamChat } from "../api";
import Markdown from "./Markdown.vue";
const emit = defineEmits(["changed", "navigate"]);
const questions = ref<any[]>([]),
  answerText = ref(""),
  focused = ref(false);
let questionTimer: ReturnType<typeof setInterval>;
async function answer(id: string, text: string) {
  try {
    await api("/questions/" + id + "/answer", json("POST", { answer: text }));
    answerText.value = "";
    questions.value = questions.value.filter((q) => q.id !== id);
  } catch (e: any) {
    error.value = e.message;
  }
}
async function command(text: string): Promise<string | null> {
  const r = await api(
    "/commands",
    json("POST", {
      text,
      session_id: selected.value || null,
      permission: permission.value,
    }),
  );
  if (r.kind === "prompt") {
    if (r.permission) permission.value = r.permission;
    return r.prompt;
  }
  if (r.kind === "navigate") emit("navigate", r.page);
  else if (r.kind === "permission") permission.value = r.permission;
  else if (r.kind === "session") {
    await refresh();
    await select(r.session_id);
  } else if (r.kind === "copy") await navigator.clipboard.writeText(r.text);
  else if (r.kind === "export") {
    const url = URL.createObjectURL(
      new Blob([r.text], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = r.filename;
    a.click();
    URL.revokeObjectURL(url);
  } else if (r.kind === "ui") {
    if (r.action === "stop") controller?.abort();
    else if (r.action === "exit") emit("navigate", "overview");
    else if (r.action === "retry")
      return (
        [...messages.value].reverse().find((m) => m.role === "user")?.content ||
        null
      );
    else if (r.action === "paste")
      input.value = await navigator.clipboard.readText();
    else if (["focus", "unfocus"].includes(r.action))
      focused.value = r.action === "focus";
    else if (["theme", "color"].includes(r.action)) {
      document.documentElement.classList.toggle("cool-theme");
    } else if (r.action === "templates")
      messages.value.push({
        role: "assistant",
        content:
          "可用工作流：`/review` 代码审查、`/plan` 制定计划、`/explain` 解释代码、`/test` 运行测试、`/parallel 任务一 || 任务二` 并行协作。",
      });
  } else
    messages.value.push({
      role: "assistant",
      content: "```json\n" + JSON.stringify(r.result ?? r, null, 2) + "\n```",
    });
  input.value = "";
  emit("changed");
  await bottom();
  return null;
}
const sessions = ref<any[]>([]),
  selected = ref(""),
  messages = ref<any[]>([]),
  input = ref(""),
  busy = ref(false),
  error = ref(""),
  permission = ref("read-only"),
  log = ref<any[]>([]),
  scroll = ref<HTMLElement>(),
  runStatus = ref("");
let controller: AbortController | null = null;
async function refresh() {
  sessions.value = await api("/sessions");
}
async function select(id: string) {
  if (busy.value) return;
  try {
    const s = await api("/sessions/" + id);
    selected.value = id;
    messages.value = s.messages.filter((m: any) => m.role !== "tool");
    log.value = [];
    error.value = "";
    runStatus.value = "";
  } catch (e: any) {
    error.value = e.message;
  }
}
function newChat() {
  if (!busy.value) {
    selected.value = "";
    messages.value = [];
    log.value = [];
    error.value = "";
    runStatus.value = "";
  }
}
async function bottom() {
  await nextTick();
  scroll.value?.scrollTo({
    top: scroll.value.scrollHeight,
    behavior: "smooth",
  });
}
async function send(text = input.value) {
  if (!text.trim() || busy.value) return;
  if (text.trim().startsWith("/")) {
    try {
      const prompt = await command(text);
      if (!prompt) return;
      text = prompt;
    } catch (e: any) {
      error.value = e.message;
      return;
    }
  }
  busy.value = true;
  error.value = "";
  runStatus.value = "";
  log.value = [];
  input.value = "";
  controller = new AbortController();
  try {
    if (!selected.value)
      selected.value = (await api("/sessions", json("POST"))).id;
    messages.value.push({ role: "user", content: text });
    await bottom();
    let index = -1;
    await streamChat(
      {
        session_id: selected.value,
        message: text,
        permission: permission.value,
      },
      controller.signal,
      (e) => {
        if (e.type === "turn_start") {
          index = messages.value.length;
          messages.value.push({
            role: "assistant",
            content: "",
            loading: true,
          });
        }
        if (e.type === "delta" && index >= 0)
          messages.value[index].content += e.text;
        if (e.type === "assistant" && index >= 0)
          messages.value[index] = { ...e.message, loading: false };
        if (e.type.startsWith("tool_") || e.type === "context_trimmed")
          log.value.push(e);
        if (e.type === "error") error.value = e.message;
        if (e.type === "run_end") {
          runStatus.value = e.status;
          if (e.error) error.value = e.error;
        }
        bottom();
      },
    );
  } catch (e: any) {
    error.value =
      e.name === "AbortError"
        ? "已停止本次生成。已完成的工具操作不会撤销。"
        : e.message;
  } finally {
    busy.value = false;
    messages.value.forEach((m) => (m.loading = false));
    try {
      await refresh();
    } catch {}
    emit("changed");
  }
}
onMounted(() => {
  refresh().catch((e) => (error.value = e.message));
  questionTimer = setInterval(async () => {
    if (selected.value && busy.value) {
      try {
        questions.value = await api("/questions?session_id=" + selected.value);
      } catch {}
    } else questions.value = [];
  }, 1000);
});
onBeforeUnmount(() => {
  controller?.abort();
  clearInterval(questionTimer);
});
</script>
<template>
  <div class="chat-layout" :class="{ 'focus-chat': focused }">
    <aside class="session-panel">
      <button class="btn primary full" :disabled="busy" @click="newChat">
        <Plus :size="16" /> 新建对话
      </button>
      <div class="eyebrow session-label">对话历史 · {{ sessions.length }}</div>
      <button
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ chosen: selected === s.id }"
        :disabled="busy"
        @click="select(s.id)"
      >
        <span>{{ s.title }}</span
        ><ChevronRight :size="14" />
      </button>
      <p v-if="!sessions.length" class="muted small">
        你的第一段对话，<br />从这里开始。
      </p>
      <div class="session-note">
        <ShieldCheck :size="17" /><span
          >会话与运行记录<br />保存在本机 SQLite</span
        >
      </div>
    </aside>
    <section class="chat-main">
      <div class="chat-scroll" ref="scroll">
        <div v-if="!messages.length" class="chat-welcome">
          <div class="welcome-symbol"><Sparkles :size="26" /></div>
          <span class="eyebrow">YOUR THINKING PARTNER</span>
          <h2>从一个问题开始。</h2>
          <p>理解代码、检索知识，让 Agent 帮你完成下一步。</p>
          <div class="starter-grid">
            <button
              @click="
                send('请列出当前工作区文件，并告诉我可以从哪里开始学习。')
              "
            >
              <FileCode2 :size="20" /><strong>探索工作区</strong
              ><span>读取真实文件，理解项目结构</span
              ><ChevronRight :size="16" />
            </button>
            <button
              @click="
                send(
                  '请调用 retrieve_context，检索知识库中 Agent 循环与 Qdrant 的作用，并引用文档来源。',
                )
              "
            >
              <BookOpen :size="20" /><strong>询问知识库</strong
              ><span>检索文档，让回答有据可查</span><ChevronRight :size="16" />
            </button>
          </div>
        </div>
        <article
          v-for="(m, i) in messages"
          :key="i"
          class="message"
          :class="m.role"
        >
          <div class="message-avatar">{{ m.role === "user" ? "你" : "M" }}</div>
          <div class="message-body">
            <div class="message-label">
              {{ m.role === "user" ? "你" : "MyClaude"
              }}<span v-if="m.role !== 'user'">AGENT</span>
            </div>
            <Markdown v-if="m.content" :text="m.content" /><span
              v-else-if="m.loading"
              class="thinking"
              ><LoaderCircle :size="14" class="spin" />
              正在思考与选择工具…</span
            >
            <div
              v-for="call in m.tool_calls || []"
              class="tool-chip"
              :key="call.id"
            >
              <FileCode2 :size="13" />{{ call.function.name }}
            </div>
          </div>
        </article>
        <div v-if="log.length" class="trace-box">
          <details>
            <summary>
              本次执行轨迹
              <span
                >{{
                  log.filter((e) => e.type === "tool_result").length
                }}
                次工具调用</span
              >
            </summary>
            <div v-for="(event, i) in log" :key="i" class="trace-row">
              <span class="dot" :class="{ bad: event.is_error }"></span>
              <div>
                <strong>{{ event.name || "上下文裁剪" }}</strong
                ><span class="muted small">
                  · {{ event.type === "tool_start" ? "调用" : "结果" }}</span
                >
                <pre>{{
                  event.output
                    ? JSON.stringify(event.output, null, 2)
                    : event.arguments || event.message
                }}</pre>
              </div>
            </div>
          </details>
        </div>
        <div v-if="error" class="notice error">{{ error }}</div>
        <div v-if="runStatus === 'completed'" class="run-complete">
          ✓ 本次运行完成 · 详情已保存到运行记录
        </div>
      </div>
      <div class="composer-wrap">
        <div v-for="q in questions" :key="q.id" class="question-card">
          <strong>{{ q.question }}</strong>
          <div class="action-row">
            <button
              v-for="option in q.options"
              :key="option"
              class="btn"
              @click="answer(q.id, option)"
            >
              {{ option }}
            </button>
          </div>
          <div class="action-row">
            <input
              v-model="answerText"
              placeholder="输入回答"
              aria-label="回答 Agent 的问题"
            /><button
              class="btn primary"
              :disabled="!answerText.trim()"
              @click="answer(q.id, answerText)"
            >
              提交回答
            </button>
          </div>
        </div>
        <div class="composer">
          <textarea
            v-model="input"
            placeholder="描述你的问题或任务，或输入 /help 查看命令…"
            rows="3"
            aria-label="输入消息"
            @keydown.enter.exact.prevent="send()"
            :disabled="busy"
          ></textarea>
          <div class="composer-bottom">
            <select v-model="permission" aria-label="工具权限" :disabled="busy">
              <option value="read-only">只读工作区</option>
              <option value="workspace-write">允许工作区写入</option>
              <option value="danger-full-access">
                允许执行（本机进程）
              </option></select
            ><span class="small muted">Enter 发送 · Shift + Enter 换行</span
            ><button
              v-if="busy"
              class="send stop"
              aria-label="停止生成"
              @click="controller?.abort()"
            >
              <Square :size="16" /></button
            ><button
              v-else
              class="send"
              :disabled="!input.trim()"
              aria-label="发送消息"
              @click="send()"
            >
              <ArrowUp :size="19" />
            </button>
          </div>
        </div>
        <p class="composer-foot">
          {{
            permission === "danger-full-access"
              ? "已允许本机进程、插件、网络和子 Agent 执行；没有 OS 沙箱隔离。"
              : permission === "workspace-write"
                ? "当前允许 Agent 修改工作区文件，请在运行记录中检查实际操作。"
                : "默认只读。模型可以读取文件和检索知识，所有工具操作均可追溯。"
          }}
        </p>
      </div>
    </section>
  </div>
</template>
