<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import {
  LayoutDashboard,
  MessageSquare,
  BookOpen,
  Activity,
  Settings2,
  ArrowUpRight,
  ArrowRight,
  Sparkles,
  PanelLeftClose,
  Workflow,
  CircleCheck,
  Database,
  Terminal,
  Layers,
  FileCode2,
  Cpu,
  ChevronRight,
  Plug,
  Users,
} from "lucide-vue-next";
import { api, apiBase } from "./api";
import FeaturesPanel from "./components/FeaturesPanel.vue";
import ShareView from "./components/ShareView.vue";
const shareToken = new URLSearchParams(location.search).get("share");
let notificationTimer: ReturnType<typeof setInterval>;
const announced = new Set<string>();
async function notify() {
  try {
    const data = await api("/features");
    if (
      !data.preferences.notifications ||
      !data.preferences.desktop_notifications ||
      !("Notification" in window) ||
      Notification.permission !== "granted"
    )
      return;
    for (const n of data.notifications) {
      if (n.read || announced.has(n.id)) continue;
      announced.add(n.id);
      new Notification("MyClaude 运行结束", { body: n.status, tag: n.id });
    }
  } catch {}
}
onBeforeUnmount(() => clearInterval(notificationTimer));
import ChatPanel from "./components/ChatPanel.vue";
import SettingsPanel from "./components/SettingsPanel.vue";
import KnowledgePanel from "./components/KnowledgePanel.vue";
import RunsPanel from "./components/RunsPanel.vue";
import ExtensionsPanel from "./components/ExtensionsPanel.vue";
import TasksPanel from "./components/TasksPanel.vue";
import RuntimePanel from "./components/RuntimePanel.vue";
const pages = [
  { id: "overview", label: "概览", icon: LayoutDashboard, en: "Overview" },
  {
    id: "chat",
    label: "Agent 工作台",
    icon: MessageSquare,
    en: "Agent workspace",
  },
  { id: "knowledge", label: "知识库", icon: BookOpen, en: "Knowledge base" },
  { id: "runs", label: "运行记录", icon: Activity, en: "Run history" },
  { id: "extensions", label: "扩展中心", icon: Plug, en: "Extensions" },
  { id: "tasks", label: "任务与协作", icon: Users, en: "Tasks" },
  { id: "runtime", label: "会话与运行时", icon: Terminal, en: "Runtime" },
  { id: "features", label: "功能工作台", icon: Workflow, en: "Features" },
  { id: "settings", label: "设置", icon: Settings2, en: "Settings" },
];
const page = ref("overview"),
  overview = ref<any>(null),
  runs = ref<any[]>([]),
  error = ref(""),
  collapsed = ref(false);
const current = computed(() => pages.find((p) => p.id === page.value)!);
async function refresh() {
  try {
    [overview.value, runs.value] = await Promise.all([
      api("/overview"),
      api("/runs"),
    ]);
    error.value = "";
  } catch (e: any) {
    error.value = e.message;
  }
}
function go(id: string) {
  page.value = id;
  window.scrollTo({ top: 0, behavior: "instant" });
  refresh();
}
const statCards = computed(() => [
  {
    label: "会话",
    value: overview.value?.sessions ?? "—",
    icon: MessageSquare,
    note: "保留每一次思考",
  },
  {
    label: "Agent 运行",
    value: overview.value?.runs ?? "—",
    icon: Workflow,
    note: "从意图到工具执行",
  },
  {
    label: "知识片段",
    value: overview.value?.chunks ?? "—",
    icon: Layers,
    note: `来自 ${overview.value?.documents ?? 0} 份文档`,
  },
  {
    label: "累计 Tokens",
    value: overview.value
      ? (
          overview.value.input_tokens + overview.value.output_tokens
        ).toLocaleString()
      : "—",
    icon: Cpu,
    note: "实际接口返回的用量",
  },
]);
onMounted(() => {
  refresh();
  notificationTimer = setInterval(notify, 10000);
});
</script>
<template>
  <ShareView v-if="shareToken" :token="shareToken" />
  <div v-else class="app-shell" :class="{ collapsed }">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="go('overview')"
        ><div class="brand-mark">m<span>✳</span></div>
        <div><strong>myclaude</strong><small>AGENT STUDIO</small></div></a
      >
      <div class="sidebar-section">WORKSPACE</div>
      <nav>
        <button
          v-for="p in pages"
          :key="p.id"
          :class="{ active: page === p.id }"
          @click="go(p.id)"
          :title="p.label"
        >
          <component :is="p.icon" :size="19" /><span>{{ p.label }}</span
          ><span v-if="p.id === 'chat'" class="nav-key">↗</span>
        </button>
      </nav>
      <div class="sidebar-bottom">
        <div class="local-card">
          <span class="live-dot"></span>
          <div>
            <strong>本地工作空间</strong><small>Python + Vue · v0.8.0</small>
          </div>
        </div>
        <div class="sidebar-footer">
          <span>Build. Learn. Iterate.</span
          ><button
            class="icon-btn"
            aria-label="收起侧栏"
            @click="collapsed = !collapsed"
          >
            <PanelLeftClose :size="17" />
          </button>
        </div>
      </div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div>
          <span class="muted">工作空间</span
          ><ChevronRight :size="14" /><strong>{{ current.label }}</strong>
        </div>
        <div class="topbar-right">
          <span class="model-pill"
            ><span
              class="live-dot"
              :class="{ off: !overview?.configured }"
            ></span
            >{{ overview?.model || "连接中" }}</span
          ><a :href="apiBase + '/docs'" target="_blank" class="api-link"
            >API 文档 <ArrowUpRight :size="13" /></a
          ><span class="avatar">CS</span>
        </div>
      </header>
      <main :class="{ 'chat-page': page === 'chat' }">
        <div v-if="error" class="notice error">
          {{ error }} <button class="text-btn" @click="refresh">重试</button>
        </div>
        <template v-if="page === 'overview'">
          <section class="hero">
            <div>
              <span class="eyebrow">A WORKSPACE FOR YOUR NEXT IDEA</span>
              <h1>把想法，<br />变成可执行的行动<span>。</span></h1>
              <p>
                与你的模型一起思考，用工具连接真实工作。<br />一个看得见过程的
                Agent 开发与学习空间。
              </p>
              <button class="btn primary" @click="go('chat')">
                开始一段新对话 <ArrowRight :size="17" />
              </button>
            </div>
            <div class="hero-art" aria-label="模型、工具和知识库协作示意">
              <div class="art-orbit orbit-one"></div>
              <div class="art-orbit orbit-two"></div>
              <div class="art-center">
                <Sparkles :size="33" /><span>AGENT</span>
              </div>
              <div class="art-node node-model">
                <Cpu :size="18" /><span>理解与推理</span>
              </div>
              <div class="art-node node-tool">
                <Terminal :size="18" /><span>工具执行</span>
              </div>
              <div class="art-node node-rag">
                <BookOpen :size="18" /><span>知识检索</span>
              </div>
              <span class="art-caption">THINK → ACT → OBSERVE</span>
            </div>
          </section>
          <section class="stat-grid">
            <div v-for="s in statCards" class="stat-card">
              <div>
                <span>{{ s.label }}</span
                ><component :is="s.icon" :size="18" />
              </div>
              <strong>{{ s.value }}</strong>
              <p>{{ s.note }}</p>
            </div>
          </section>
          <section class="overview-grid">
            <div class="panel quick-panel">
              <div class="section-head">
                <h3>从这里开始</h3>
                <span class="eyebrow">01 / EXPLORE</span>
              </div>
              <button class="quick-row" @click="go('chat')">
                <div class="quick-icon"><FileCode2 :size="20" /></div>
                <div>
                  <strong>探索你的代码</strong>
                  <p>让 Agent 读取文件，解释实现，协助修改</p>
                </div>
                <ArrowUpRight :size="18" /></button
              ><button class="quick-row" @click="go('knowledge')">
                <div class="quick-icon peach"><BookOpen :size="20" /></div>
                <div>
                  <strong>连接你的知识</strong>
                  <p>为回答提供相关资料与可追溯的来源</p>
                </div>
                <ArrowUpRight :size="18" /></button
              ><button class="quick-row" @click="go('settings')">
                <div class="quick-icon lavender"><Settings2 :size="20" /></div>
                <div>
                  <strong>调整你的 Agent</strong>
                  <p>选择模型，定义提示词和执行边界</p>
                </div>
                <ArrowUpRight :size="18" />
              </button>
            </div>
            <div class="panel system-panel">
              <div class="section-head">
                <h3>工作空间状态</h3>
                <span class="badge">LOCAL</span>
              </div>
              <div class="system-row">
                <span><Cpu :size="16" />对话模型</span
                ><strong>{{ overview?.model || "—" }}</strong>
              </div>
              <div class="system-row">
                <span><Database :size="16" />会话存储</span
                ><strong>SQLite <i class="live-dot"></i></strong>
              </div>
              <div class="system-row">
                <span><CircleCheck :size="16" />默认权限</span
                ><strong>只读工作区</strong>
              </div>
              <div class="workspace-path">
                <span class="eyebrow">WORKING DIRECTORY</span>
                <p>{{ overview?.workspace || "—" }}</p>
              </div>
              <div class="subtle-note">
                模型提出行动，运行时检查权限并执行。每一步工具调用都可以在运行记录中查看。
              </div>
            </div>
          </section>
          <section class="panel recent-panel">
            <div class="section-head">
              <h3>
                最近运行 <span class="small muted">/ RECENT ACTIVITY</span>
              </h3>
              <button class="text-btn" @click="go('runs')">
                查看全部 <ArrowRight :size="14" />
              </button>
            </div>
            <div v-if="!runs.length" class="empty-inline">
              <Activity :size="22" />
              <div>
                <strong>等待你的第一次运行</strong>
                <p>发送一个问题，Agent 的执行过程会出现在这里。</p>
              </div>
              <button class="btn" @click="go('chat')">开始对话</button>
            </div>
            <div
              v-for="r in runs.slice(0, 3)"
              :key="r.id"
              class="recent-row"
              @click="go('runs')"
            >
              <span
                class="dot"
                :class="{ bad: r.status !== 'completed' }"
              ></span
              ><strong>{{ r.title || "新对话" }}</strong
              ><span
                >{{ r.turns }} 轮 ·
                {{ r.input_tokens + r.output_tokens }} tokens</span
              ><span class="status" :class="r.status">{{ r.status }}</span
              ><ArrowUpRight :size="14" />
            </div>
          </section>
          <footer class="page-footer">
            <span>MYCLAUDE / PYTHON REBUILD</span
            ><span>让每一次运行，都成为一次学习。</span>
          </footer>
        </template>
        <ChatPanel v-if="page === 'chat'" @changed="refresh" @navigate="go" />
        <ExtensionsPanel v-if="page === 'extensions'" />
        <FeaturesPanel v-if="page === 'features'" @navigate="go" />
        <TasksPanel v-if="page === 'tasks'" />
        <RuntimePanel v-if="page === 'runtime'" />
        <KnowledgePanel v-if="page === 'knowledge'" @changed="refresh" />
        <RunsPanel v-if="page === 'runs'" />
        <SettingsPanel v-if="page === 'settings'" @changed="refresh" />
      </main>
    </div>
  </div>
</template>
