<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, json, apiBase } from "../api";
const tab = ref("sessions"),
  busy = ref(false),
  error = ref(""),
  output = ref("");
const sessions = ref<any[]>([]),
  selected = ref(""),
  skills = ref<any[]>([]),
  agents = ref<any[]>([]),
  auth = ref<any>({});
const source = ref(""),
  agentName = ref(""),
  agentPrompt = ref(""),
  callback = ref(""),
  authorizeUrl = ref("");
const oauth = ref({
  authorize_url: "",
  token_url: "",
  client_id: "",
  scopes: [] as string[],
  redirect_uri: `${window.location.origin}${apiBase}/oauth/callback`,
});
const scopeText = ref(""),
  allowed = ref(""),
  denied = ref(""),
  authMode = ref("api-key");
const hooksJson = ref("{}"),
  lspJson = ref("{}"),
  retries = ref(2);
const reasoning = ref(""),
  rulesJson = ref("{}"),
  configHome = ref("");
const compactMode = ref("standard");
const diagnostic = ref("recovery"),
  diagnosticJson = ref(
    '{"scenario":"stale_branch","fail_at_step":1,"attempts":2}',
  );
function example() {
  const examples: Record<string, any> = {
    recovery: { scenario: "stale_branch", fail_at_step: 1, attempts: 2 },
    branch: { branch: "HEAD", main_ref: "main", policy: "warn_only" },
    collisions: {
      intents: [
        { laneId: "one", branch: "work", modules: ["backend"] },
        { laneId: "two", branch: "work", modules: ["backend/app"] },
      ],
    },
    policy: {
      rules: [
        {
          name: "retry",
          priority: 1,
          condition: "startup_blocked",
          action: "recover_once",
        },
      ],
      context: { lane_id: "demo", blocker: "startup" },
    },
    report: {
      generated_at: new Date().toISOString(),
      producer: "learning",
      claims: [
        {
          id: "result-1",
          kind: "observed_fact",
          text: "This is a learning fixture",
          confidence: "high",
          sensitivity: "public",
          evidence: ["fixture"],
        },
      ],
    },
    usage: {
      messages: [
        {
          usage: {
            input_tokens: 100,
            output_tokens: 20,
            cache_read_input_tokens: 40,
          },
        },
      ],
      rates: {
        input_tokens: 1,
        output_tokens: 2,
        cache_read_input_tokens: 0.1,
      },
    },
  };
  diagnosticJson.value = JSON.stringify(examples[diagnostic.value], null, 2);
}
async function load() {
  const [s, sk, ag, au, cfg] = await Promise.all([
    api("/sessions"),
    api("/assets/skills"),
    api("/assets/agents"),
    api("/oauth"),
    api("/settings"),
  ]);
  sessions.value = s;
  skills.value = sk;
  agents.value = ag;
  auth.value = au;
  if (!selected.value && s.length) selected.value = s[0].id;
  allowed.value = cfg.allowed_tools.join(", ");
  denied.value = cfg.denied_tools.join(", ");
  authMode.value = cfg.auth_mode;
  hooksJson.value = JSON.stringify(cfg.hooks, null, 2);
  lspJson.value = JSON.stringify(cfg.lsp_servers, null, 2);
  retries.value = cfg.provider_retries;
  reasoning.value = cfg.reasoning_effort || "";
  rulesJson.value = JSON.stringify(cfg.permission_rules || {}, null, 2);
  if (au.config) {
    oauth.value = au.config;
    scopeText.value = au.config.scopes.join(" ");
  }
}
async function perform(work: () => Promise<any>) {
  busy.value = true;
  error.value = "";
  try {
    const result = await work();
    output.value = JSON.stringify(result, null, 2);
    await load();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function download(format: string) {
  await perform(async () => {
    const result = await api(
      `/sessions/${selected.value}/export?format=${format}`,
    );
    const url = URL.createObjectURL(
      new Blob([result.text], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `session-${selected.value}.${format}`;
    link.click();
    URL.revokeObjectURL(url);
    return { exported: selected.value, format };
  });
}
async function importFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  await perform(async () =>
    api("/sessions/import", json("POST", { text: await file.text() })),
  );
}
async function beginOAuth() {
  await perform(async () => {
    const r = await api(
      "/oauth/start",
      json("POST", {
        ...oauth.value,
        scopes: scopeText.value.split(/\s+/).filter(Boolean),
      }),
    );
    authorizeUrl.value = r.authorize_url;
    return { message: "请打开授权页面完成登录" };
  });
}
onMounted(() => load().catch((e) => (error.value = e.message)));
</script>
<template>
  <div class="page-intro">
    <div>
      <span class="eyebrow">RUNTIME CONTROL</span>
      <h2>会话与运行时</h2>
      <p>管理可恢复的上下文、可复用的技能，以及模型登录方式。</p>
    </div>
    <button class="btn" :disabled="busy" @click="perform(load)">刷新</button>
  </div>
  <div class="tabs">
    <button
      v-for="t in [
        { id: 'sessions', name: '会话迁移' },
        { id: 'assets', name: '技能与 Agent' },
        { id: 'oauth', name: 'OAuth 登录' },
        { id: 'policy', name: '工具策略' },
        { id: 'diagnostics', name: '运行时诊断' },
      ]"
      :class="{ active: tab === t.id }"
      @click="tab = t.id"
    >
      {{ t.name }}
    </button>
  </div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div>
  <section class="runtime-card" v-if="tab === 'sessions'">
    <h3>导入与导出</h3>
    <p>
      支持 Rust 会话 JSON / JSONL 和当前 Python
      会话。工具调用与结果成对保留；导入创建新副本。
    </p>
    <label
      >当前会话<select v-model="selected">
        <option v-for="s in sessions" :value="s.id">
          {{ s.title }} · {{ s.id.slice(0, 8) }}
        </option>
      </select></label
    >
    <label
      >压缩方式<select v-model="compactMode">
        <option value="standard">标准摘要</option>
        <option value="trident">三阶段整理后摘要</option>
      </select></label
    >
    <div class="runtime-actions">
      <button
        class="btn"
        :disabled="busy || !selected"
        @click="download('json')"
      >
        导出 JSON</button
      ><button
        class="btn"
        :disabled="busy || !selected"
        @click="download('jsonl')"
      >
        导出 JSONL</button
      ><button
        class="btn"
        :disabled="busy || !selected"
        @click="
          perform(() => api(`/sessions/${selected}/fork`, json('POST', {})))
        "
      >
        创建会话分支</button
      ><button
        class="btn"
        :disabled="busy || !selected"
        @click="
          perform(() =>
            api(
              `/sessions/${selected}/compact`,
              json('POST', { preserve: 4, mode: compactMode }),
            ),
          )
        "
      >
        压缩早期上下文
      </button>
    </div>
    <label
      >导入会话文件<input
        type="file"
        accept=".json,.jsonl"
        :disabled="busy"
        @change="importFile"
    /></label>
  </section>
  <section class="runtime-card" v-if="tab === 'assets'">
    <h3>安装技能</h3>
    <p>选择本机 SKILL.md 或技能目录。已安装定义可通过 Skill 工具调用。</p>
    <label
      >源路径<input v-model="source" placeholder="C:/path/to/skill" /></label
    ><button
      class="btn primary"
      :disabled="busy || !source"
      @click="perform(() => api('/skills/install', json('POST', { source })))"
    >
      安装技能
    </button>
    <div class="runtime-row" v-for="s in skills">
      <span
        ><b>{{ s.name }}</b
        ><small>{{ s.scope }} · {{ s.path }}</small></span
      ><button
        v-if="s.scope === 'user'"
        class="btn"
        :disabled="busy"
        @click="perform(() => api(`/skills/${s.name}/remove`, json('POST')))"
      >
        卸载
      </button>
    </div>
    <h3>创建 Agent 定义</h3>
    <label>名称<input v-model="agentName" placeholder="code-reviewer" /></label
    ><label
      >职责与指令<textarea
        v-model="agentPrompt"
        rows="4"
        placeholder="描述这个 Agent 的任务边界与工作方法"
      /></label
    ><button
      class="btn primary"
      :disabled="busy || !agentName"
      @click="
        perform(() =>
          api(
            '/agents/create',
            json('POST', { name: agentName, prompt: agentPrompt }),
          ),
        )
      "
    >
      创建 Agent
    </button>
    <div class="runtime-row" v-for="a in agents">
      <span
        ><b>{{ a.name }}</b
        ><small>{{ a.path }}</small></span
      ><code>/agents run {{ a.name }} 任务说明</code>
    </div>
  </section>
  <section class="runtime-card" v-if="tab === 'oauth'">
    <h3>
      {{ auth.authenticated ? "已保存 OAuth 凭据" : "配置 OAuth 授权服务" }}
    </h3>
    <p>
      填写云服务提供的授权端点、令牌端点和客户端
      ID。登录完成后，在工具策略页切换认证方式。
    </p>
    <label
      >授权端点<input
        v-model="oauth.authorize_url"
        placeholder="https://provider.example/oauth/authorize" /></label
    ><label
      >令牌端点<input
        v-model="oauth.token_url"
        placeholder="https://provider.example/oauth/token" /></label
    ><label>Client ID<input v-model="oauth.client_id" /></label
    ><label>Scopes（空格分隔）<input v-model="scopeText" /></label
    ><label>回调地址<input v-model="oauth.redirect_uri" /></label>
    <div class="runtime-actions">
      <button
        class="btn primary"
        :disabled="busy || !oauth.client_id"
        @click="beginOAuth"
      >
        开始授权</button
      ><button
        class="btn"
        :disabled="busy || !auth.refreshable"
        @click="perform(() => api('/oauth/refresh', json('POST')))"
      >
        刷新令牌</button
      ><button
        class="btn"
        :disabled="busy || !auth.authenticated"
        @click="perform(() => api('/oauth/logout', json('POST')))"
      >
        退出登录
      </button>
    </div>
    <a
      v-if="authorizeUrl"
      class="btn"
      :href="authorizeUrl"
      target="_blank"
      rel="noreferrer"
      >打开云服务授权页面 ↗</a
    >
    <label
      >手动回调（服务未自动跳转时）<input
        v-model="callback"
        placeholder="粘贴完整回调 URL，完成后清空" /></label
    ><button
      class="btn"
      :disabled="busy || !callback"
      @click="
        perform(async () => {
          const r = await api('/oauth/complete', json('POST', { callback }));
          callback = '';
          return r;
        })
      "
    >
      完成登录
    </button>
  </section>
  <section class="runtime-card" v-if="tab === 'policy'">
    <h3>认证与工具范围</h3>
    <label
      >对话模型认证<select v-model="authMode">
        <option value="api-key">API Key</option>
        <option value="oauth">OAuth</option>
      </select></label
    >
    <p>
      工具名单进一步限制会话权限。逗号分隔，支持 *
      通配符；拒绝名单优先。允许名单为空时使用会话已有权限。
    </p>
    <label
      >允许的工具<input
        v-model="allowed"
        placeholder="read_file, glob_search, mcp__learning__*" /></label
    ><label
      >拒绝的工具<input
        v-model="denied"
        placeholder="bash, PowerShell" /></label
    ><button
      class="btn primary"
      :disabled="busy"
      @click="
        perform(() =>
          api(
            '/settings',
            json('PUT', {
              auth_mode: authMode,
              allowed_tools: allowed
                .split(',')
                .map((x) => x.trim())
                .filter(Boolean),
              denied_tools: denied
                .split(',')
                .map((x) => x.trim())
                .filter(Boolean),
            }),
          ),
        )
      "
    >
      保存策略
    </button>
    <h3 style="margin-top: 32px">语言服务器与 Hooks</h3>
    <p>
      Python 语言服务器已内置。其他语言填写已安装的服务器命令数组，例如
      {"rust":["rust-analyzer"]}。Hook
      在执行权限模式下运行，具体结构见使用文档。
    </p>
    <label>LSP 配置（JSON）<textarea v-model="lspJson" rows="5" /></label
    ><label>Hooks（JSON）<textarea v-model="hooksJson" rows="7" /></label
    ><label
      >模型连接重试次数<input
        type="number"
        min="0"
        max="5"
        v-model.number="retries" /></label
    ><button
      class="btn primary"
      :disabled="busy"
      @click="
        perform(() =>
          api(
            '/settings',
            json('PUT', {
              lsp_servers: JSON.parse(lspJson),
              hooks: JSON.parse(hooksJson),
              provider_retries: retries,
            }),
          ),
        )
      "
    >
      保存运行时配置
    </button>
    <h3 style="margin-top: 32px">参数规则与模型推理</h3>
    <p>
      deny 优先于 allow，ask
      请求用户确认。规则不能扩大当前会话的权限。推理强度只用于支持
      reasoning_effort 的模型。
    </p>
    <label
      >权限规则（JSON）<textarea
        v-model="rulesJson"
        rows="5"
        placeholder='{"deny":["bash(rm:*)"],"ask":["write_file(*)"]}'
      />
    </label>
    <label
      >推理强度<select v-model="reasoning">
        <option value="">服务商默认</option>
        <option
          v-for="level in ['none', 'minimal', 'low', 'medium', 'high', 'xhigh']"
          :value="level"
        >
          {{ level }}
        </option>
      </select></label
    ><button
      class="btn primary"
      :disabled="busy"
      @click="
        perform(() =>
          api(
            '/settings',
            json('PUT', {
              permission_rules: JSON.parse(rulesJson),
              reasoning_effort: reasoning || null,
            }),
          ),
        )
      "
    >
      保存规则与推理设置
    </button>
  </section>
  <section class="runtime-card" v-if="tab === 'diagnostics'">
    <h3>查看运行时决策</h3>
    <p>
      输入测试数据，观察恢复次数、分支策略、报告脱敏和用量计算。恢复与策略评估不会执行重启、合并或推送。
    </p>
    <label
      >评估类型<select v-model="diagnostic" @change="example">
        <option value="recovery">失败恢复仿真</option>
        <option value="branch">真实 Git 分支状态</option>
        <option value="collisions">分支修改冲突</option>
        <option value="policy">策略规则</option>
        <option value="report">标准化报告</option>
        <option value="usage">Token 与自定义单价</option>
      </select></label
    ><label>输入 JSON<textarea v-model="diagnosticJson" rows="10" /></label
    ><button
      class="btn primary"
      :disabled="busy"
      @click="
        perform(() =>
          api(
            `/runtime/evaluate/${diagnostic}`,
            json('POST', JSON.parse(diagnosticJson)),
          ),
        )
      "
    >
      执行评估
    </button>
    <h3 style="margin-top: 32px">原项目分层配置</h3>
    <p>
      按用户、项目、本机顺序读取五层 .claw 配置，并显示覆盖来源。预览不会执行
      Hooks。存在未映射字段时，导入会停止并列出字段。
    </p>
    <label
      >原配置根目录（可选）<input
        v-model="configHome"
        placeholder="留空使用 CLAW_CONFIG_HOME 或用户目录/.claw"
    /></label>
    <div class="runtime-actions">
      <button
        class="btn"
        :disabled="busy"
        @click="
          perform(() =>
            api(
              '/runtime/config-layers',
              json('POST', { config_home: configHome || null }),
            ),
          )
        "
      >
        预览配置</button
      ><button
        class="btn"
        :disabled="busy"
        @click="
          perform(() =>
            api(
              '/runtime/config-layers',
              json('POST', { config_home: configHome || null, apply: true }),
            ),
          )
        "
      >
        导入兼容配置
      </button>
    </div>
  </section>
  <details v-if="output" class="runtime-card" open>
    <summary>操作结果</summary>
    <pre>{{ output }}</pre>
  </details>
</template>
<style scoped>
.runtime-card {
  background: var(--surface, #fff);
  border: 1px solid var(--border, #ddd);
  border-radius: 16px;
  padding: 26px;
  margin-top: 20px;
  max-width: 1100px;
}
.runtime-card p {
  color: var(--muted, #666);
  line-height: 1.7;
}
.runtime-card label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 18px 0;
  font-size: 13px;
}
.runtime-card input,
.runtime-card select,
.runtime-card textarea {
  padding: 12px;
  border: 1px solid #d4d9d4;
  border-radius: 8px;
  background: #fff;
  font: inherit;
  width: 100%;
  box-sizing: border-box;
}
.runtime-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 20px 0;
}
.runtime-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  border-top: 1px solid #e7e9e7;
  padding: 16px 0;
  margin-top: 14px;
}
.runtime-row small {
  display: block;
  color: #68746a;
  font-size: 12px;
  margin-top: 6px;
  overflow-wrap: anywhere;
}
.runtime-card pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 480px;
  overflow: auto;
  font-size: 12px;
}
.runtime-card h3 {
  margin-top: 8px;
}
.runtime-row code {
  font-size: 12px;
}
</style>
