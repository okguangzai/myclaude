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
  Trash2,
} from "lucide-vue-next";
import { api, json, streamChat } from "../api";
import Markdown from "./Markdown.vue";
import ComposerEditor from "./ComposerEditor.vue";
import {
  uploadImage,
  mediaUrl,
  screenshot,
  recordVoice,
  installDesktop,
} from "../media";
const preferences = ref<any>({
  vim: false,
  voice: false,
  advisor: false,
  keybindings: { send: "Enter", new: "Alt+n", stop: "Escape", focus: "Alt+i" },
});
const attachments = ref<any[]>([]),
  filePicker = ref<HTMLInputElement>(),
  editor = ref<any>(),
  recording = ref(false),
  stickerPacks = ref<any[]>([]),
  playing = ref("");
let recorder: Awaited<ReturnType<typeof recordVoice>> | null = null;
async function guarded(action: () => Promise<any>) {
  try {
    return await action();
  } catch (e: any) {
    error.value = e.message || "操作失败";
  }
}
async function addImage(event: Event) {
  await guarded(async () => {
    for (const file of Array.from(
      (event.target as HTMLInputElement).files || [],
    )) {
      if (attachments.value.length >= 4) throw Error("最多 4 张图片");
      attachments.value.push(await uploadImage(file, file.name));
    }
    (event.target as HTMLInputElement).value = "";
  });
}
async function capture() {
  await guarded(async () => {
    if (attachments.value.length >= 4) throw Error("最多 4 张图片");
    attachments.value.push(await screenshot());
  });
}
async function voice() {
  await guarded(async () => {
    if (recorder) {
      const current = recorder;
      recorder = null;
      recording.value = false;
      input.value += await current.stop();
    } else {
      recorder = await recordVoice(() => void voice());
      recording.value = true;
    }
  });
}
async function speak() {
  await guarded(async () => {
    const text = [...messages.value]
      .reverse()
      .find(
        (m) => m.role === "assistant" && typeof m.content === "string",
      )?.content;
    if (!text) throw Error("没有可朗读的回答");
    const value = await api("/features/speech/speak", json("POST", { text }));
    playing.value = mediaUrl(value.id);
  });
}
async function featureAction(r: any) {
  if (r.kind === "attachment") {
    attachments.value.push(...r.items);
    return true;
  }
  if (r.kind === "draft") {
    input.value = r.text;
    return true;
  }
  if (r.kind !== "ui") return false;
  if (r.action === "feature-preferences") {
    preferences.value = r.preferences;
    if (r.command === "voice" && r.preferences.voice) await voice();
    else if (r.command === "voice" && recorder) {
      await recorder.cancel();
      recorder = null;
      recording.value = false;
    }
    return true;
  }
  if (r.action === "image") {
    filePicker.value?.click();
    return true;
  }
  if (r.action === "screenshot") {
    await capture();
    return true;
  }
  if (r.action === "listen") {
    await voice();
    return true;
  }
  if (r.action === "speak") {
    playing.value = mediaUrl(r.audio.id);
    return true;
  }
  if (r.action === "stickers") {
    stickerPacks.value = r.packs;
    return true;
  }
  if (r.action === "desktop") {
    await installDesktop();
    return true;
  }
  if (r.action === "ide") {
    emit("navigate", "features");
    return true;
  }
  return false;
}
function globalKey(e: KeyboardEvent) {
  const key = [
    e.ctrlKey ? "Ctrl" : "",
    e.altKey ? "Alt" : "",
    e.shiftKey ? "Shift" : "",
    e.metaKey ? "Meta" : "",
    e.key.length === 1 ? e.key.toLowerCase() : e.key,
  ]
    .filter(Boolean)
    .join("+");
  if (key === preferences.value.keybindings.focus) {
    e.preventDefault();
    editor.value?.focus();
  } else if (key === preferences.value.keybindings.stop && busy.value) {
    e.preventDefault();
    controller?.abort();
  } else if (key === preferences.value.keybindings.new && !busy.value) {
    e.preventDefault();
    newChat();
  }
}

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
  input.value = "";
  if (await featureAction(r)) return null;
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
async function removeSession(id: string) {
  if (
    busy.value ||
    !window.confirm("删除这段对话？系统会保留可恢复副本和运行记录。")
  )
    return;
  try {
    await api("/sessions/" + id, { method: "DELETE" });
    if (selected.value === id) newChat();
    await refresh();
    emit("changed");
  } catch (e: any) {
    error.value = e.message;
  }
}
async function select(id: string) {
  attachments.value = [];
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
  if (busy.value) return;
  if (!text.trim() && attachments.value.length) text = "请分析所附图片";
  if (!text.trim()) return;
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
    const sentAttachments = attachments.value.map((a) => a.id);
    messages.value.push({
      role: "user",
      content: text,
      attachments: sentAttachments,
    });
    attachments.value = [];
    await bottom();
    let index = -1;
    await streamChat(
      {
        session_id: selected.value,
        message: text,
        attachments: sentAttachments,
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
  api("/features")
    .then((r) => (preferences.value = r.preferences))
    .catch((e) => (error.value = e.message));
  window.addEventListener("keydown", globalKey);
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
  recorder?.cancel();
  window.removeEventListener("keydown", globalKey);
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
      <div
        v-for="s in sessions"
        :key="s.id"
        style="display: flex; align-items: center"
      >
        <button
          class="session-item"
          style="flex: 1; min-width: 0"
          :class="{ chosen: selected === s.id }"
          :disabled="busy"
          @click="select(s.id)"
        >
          <span>{{ s.title }}</span
          ><ChevronRight :size="14" />
        </button>
        <button
          class="btn"
          :disabled="busy"
          :aria-label="'删除对话：' + s.title"
          @click="removeSession(s.id)"
        >
          <Trash2 :size="14" />
        </button>
      </div>
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
            <div v-if="m.attachments?.length" class="action-row">
              <img
                v-for="id in m.attachments"
                :key="id"
                :src="mediaUrl(id)"
                alt="对话图片"
                style="max-width: 250px; max-height: 200px"
              />
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
        <div class="action-row media-toolbar">
          <input
            ref="filePicker"
            type="file"
            hidden
            accept="image/png,image/jpeg,image/webp"
            multiple
            @change="addImage"
          />
          <button class="btn" :disabled="busy" @click="filePicker?.click()">
            添加图片
          </button>
          <button class="btn" :disabled="busy" @click="capture">截图</button>
          <button class="btn" :disabled="busy" @click="voice">
            {{ recording ? "停止录音并转写" : "语音输入" }}
          </button>
          <button class="btn" @click="speak">朗读回答</button>
          <button class="btn" @click="guarded(() => command('/stickers'))">
            贴纸
          </button>
          <span v-if="preferences.advisor" class="status">顾问模式 · 只读</span>
        </div>
        <audio
          v-if="playing"
          :src="playing"
          controls
          autoplay
          style="width: 100%"
        ></audio>
        <div v-if="stickerPacks.length" class="action-row">
          <template v-for="pack in stickerPacks" :key="pack.id"
            ><button
              v-for="item in pack.items"
              :key="item"
              class="btn"
              @click="
                input += item;
                stickerPacks = [];
              "
            >
              {{ item }}
            </button></template
          >
        </div>
        <div class="action-row">
          <div v-for="(a, i) in attachments" :key="a.id">
            <img
              :src="mediaUrl(a.id)"
              :alt="a.name"
              style="max-height: 80px; max-width: 100px"
            /><button
              class="btn"
              :aria-label="'移除附件 ' + a.name"
              @click="attachments.splice(i, 1)"
            >
              ×
            </button>
          </div>
        </div>
        <div class="composer">
          <ComposerEditor
            ref="editor"
            v-model="input"
            :vim="preferences.vim"
            :keys="preferences.keybindings"
            :disabled="busy"
            @send="send()"
            @new="newChat"
            @stop="controller?.abort()"
          />
          <div class="composer-bottom">
            <select
              v-model="permission"
              aria-label="工具权限"
              :disabled="busy || preferences.advisor"
            >
              <option value="read-only">只读工作区</option>
              <option value="workspace-write">允许工作区写入</option>
              <option value="danger-full-access">
                允许执行（本机进程）
              </option></select
            ><span class="small muted"
              >{{ preferences.keybindings.send }} 发送 · Shift + Enter
              换行</span
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
              :disabled="!input.trim() && !attachments.length"
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
