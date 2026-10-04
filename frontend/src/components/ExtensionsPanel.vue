<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  Plug,
  Puzzle,
  Search,
  RefreshCw,
  Play,
  Plus,
  ShieldCheck,
  Terminal,
} from "lucide-vue-next";
import { api, json } from "../api";
const tab = ref("mcp"),
  data = ref<any>({ mcp: [], plugins: [] }),
  catalog = ref<any>({ tools: [], commands: [] });
const busy = ref(false),
  error = ref(""),
  output = ref(""),
  filter = ref("");
const config = ref(
  JSON.stringify(
    {
      name: "learning",
      transport: "stdio",
      command: "$python",
      args: ["C:/csg/work/project/myCalude/examples/mcp_server.py"],
      permission: "read-only",
      enabled: true,
      env_refs: {},
      header_refs: {},
      timeout: 30,
    },
    null,
    2,
  ),
);
const pluginPath = ref(
  "C:/csg/work/project/myCalude/examples/plugins/learning",
);
const toolName = ref("MCP"),
  argumentsText = ref(
    '{"server":"learning","tool":"add","arguments":{"a":2,"b":3}}',
  ),
  permission = ref("read-only");
async function refresh() {
  [data.value, catalog.value] = await Promise.all([
    api("/extensions"),
    api("/catalog"),
  ]);
}
async function perform(work: () => Promise<any>) {
  busy.value = true;
  error.value = "";
  try {
    output.value = JSON.stringify(await work(), null, 2);
    await refresh();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
function edit(server: any) {
  const { id, ...value } = server;
  config.value = JSON.stringify(value, null, 2);
}
const filtered = (list: any[]) =>
  list.filter((x) =>
    JSON.stringify(x).toLowerCase().includes(filter.value.toLowerCase()),
  );
onMounted(() => refresh().catch((e) => (error.value = e.message)));
</script>
<template>
  <section class="extension-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">CONNECT / EXTEND / INSPECT</span>
        <h1>扩展中心<span>。</span></h1>
        <p>连接 MCP 服务，安装插件，让每个工具都有明确的执行边界。</p>
      </div>
      <button class="btn" @click="perform(refresh)" :disabled="busy">
        <RefreshCw :size="16" />刷新
      </button>
    </div>
    <div class="extension-tabs">
      <button
        v-for="t in [
          { id: 'mcp', label: 'MCP 服务', icon: Plug },
          { id: 'plugins', label: '插件 & Hooks', icon: Puzzle },
          { id: 'tools', label: '工具实验室', icon: Terminal },
          { id: 'catalog', label: '迁移目录', icon: Search },
        ]"
        :key="t.id"
        :class="{ active: tab === t.id }"
        @click="tab = t.id"
      >
        <component :is="t.icon" :size="17" />{{ t.label }}
      </button>
    </div>
    <div v-if="error" class="notice error">{{ error }}</div>
    <div v-if="tab === 'mcp'" class="extension-grid">
      <div class="panel extension-editor">
        <div class="section-head">
          <h3>服务配置</h3>
          <span class="badge">STDIO / HTTP / SSE</span>
        </div>
        <p class="muted">
          命令和参数以数组传递。密钥使用 .env 中的变量名：env_refs
          给进程环境，header_refs 给 HTTP 请求头。
        </p>
        <label
          >JSON 配置<textarea
            v-model="config"
            class="code-input"
            rows="17"
            spellcheck="false"
            aria-label="MCP JSON 配置"
          ></textarea>
        </label>
        <div class="notice">
          permission
          是你赋予该服务的权限，不根据服务自报的只读标签自动放行。启用 stdio
          会启动本机进程。
        </div>
        <button
          class="btn primary"
          :disabled="busy"
          @click="perform(() => api('/mcp', json('PUT', JSON.parse(config))))"
        >
          <Plus :size="16" />保存服务
        </button>
      </div>
      <div class="extension-stack">
        <div v-for="s in data.mcp" :key="s.id" class="panel extension-card">
          <div class="section-head">
            <h3><Plug :size="18" />{{ s.name }}</h3>
            <span
              class="status"
              :class="s.enabled ? 'completed' : 'cancelled'"
              >{{ s.enabled ? "已启用" : "已禁用" }}</span
            >
          </div>
          <p>{{ s.transport }} · {{ s.permission }}</p>
          <code>{{ s.url || s.command }}</code>
          <div class="action-row">
            <button class="btn" @click="edit(s)">编辑</button
            ><button
              class="btn primary"
              :disabled="busy"
              @click="
                perform(() =>
                  api(
                    '/mcp/' + s.name + '/discover',
                    json('POST', { permission: s.permission }),
                  ),
                )
              "
            >
              连接并发现</button
            ><button
              class="text-btn"
              :disabled="busy"
              @click="
                perform(() =>
                  api('/mcp/' + s.name + '/disconnect', json('POST', {})),
                )
              "
            >
              断开</button
            ><button
              class="text-btn"
              :disabled="busy"
              @click="
                perform(() =>
                  api('/mcp/' + s.name + '/remove', json('POST', {})),
                )
              "
            >
              移除
            </button>
          </div>
        </div>
        <div v-if="!data.mcp.length" class="panel extension-card empty-inline">
          还没有服务。左侧自带的学习示例无需密钥即可验证工具、资源与提示词。
        </div>
        <div class="panel extension-card">
          <ShieldCheck :size="24" />
          <h3>先验证，再交给 Agent</h3>
          <p>
            连接后查看真实工具 schema；到工具实验室调用 MCP。启用的服务可以由
            Agent 通过 MCP 工具访问。
          </p>
        </div>
      </div>
    </div>
    <div v-if="tab === 'plugins'" class="extension-grid">
      <div class="panel extension-editor">
        <h3>安装本地插件</h3>
        <div class="section-head">
          <button
            class="btn"
            :disabled="busy"
            @click="perform(() => api('/plugins/registry'))"
          >
            查看发现与健康状态
          </button>
          <button
            class="btn"
            :disabled="busy"
            @click="perform(() => api('/plugins/sync', json('POST', {})))"
          >
            同步插件目录
          </button>
        </div>
        <p class="muted">
          兼容 .claude-plugin/plugin.json 清单。安装时复制并校验文件，默认禁用。
        </p>
        <label
          >插件目录<input v-model="pluginPath" aria-label="插件目录" /></label
        ><button
          class="btn primary"
          :disabled="busy"
          @click="
            perform(() =>
              api('/plugins/install', json('POST', { path: pluginPath })),
            )
          "
        >
          安装插件
        </button>
        <div class="notice">
          启用插件会执行 Init，关闭时执行 Shutdown。工具与 hooks
          执行本机代码，运行时需要“允许执行”权限。请先审查插件代码。
        </div>
        <h3>Hook 顺序</h3>
        <div class="flow-steps">
          <span>PreToolUse</span><b>↓</b><span>校验参数与权限</span><b>↓</b
          ><span>执行工具</span><b>↓</b><span>PostToolUse / Failure</span>
        </div>
      </div>
      <div class="extension-stack">
        <div v-for="p in data.plugins" :key="p.id" class="panel extension-card">
          <div class="section-head">
            <h3>
              {{ p.name }}
              <small class="muted">v{{ p.manifest.version }}</small>
            </h3>
            <span class="badge">{{ p.enabled ? "ENABLED" : "DISABLED" }}</span>
          </div>
          <p>{{ p.manifest.description }}</p>
          <div class="tag-row">
            <span
              v-for="(_, hook) in p.manifest.hooks"
              :key="hook"
              class="badge"
              >{{ hook }}</span
            >
          </div>
          <p class="small muted">
            {{ p.manifest.tools?.length || 0 }} 个工具 ·
            {{ p.manifest.commands?.length || 0 }} 个命令
          </p>
          <div class="action-row">
            <button
              class="btn"
              :disabled="busy"
              @click="
                perform(() =>
                  api('/plugins/' + p.id + '/update', json('POST', {})),
                )
              "
            >
              从安装源更新
            </button>
            <button
              class="btn primary"
              :disabled="busy"
              @click="
                perform(() =>
                  api(
                    '/plugins/' +
                      p.id +
                      '/' +
                      (p.enabled ? 'disable' : 'enable'),
                    json('POST', {}),
                  ),
                )
              "
            >
              {{ p.enabled ? "禁用" : "启用并运行 Init" }}</button
            ><button
              class="text-btn"
              :disabled="busy"
              @click="
                perform(() =>
                  api('/plugins/' + p.id + '/remove', json('POST', {})),
                )
              "
            >
              卸载
            </button>
          </div>
        </div>
        <p v-if="!data.plugins.length" class="muted">
          安装自带 learning 插件，体验字数统计和 .protected 文件拦截。
        </p>
        <pre v-if="data.plugin_errors?.length" class="code-output">{{
          JSON.stringify(data.plugin_errors, null, 2)
        }}</pre>
      </div>
    </div>
    <div v-if="tab === 'tools'" class="extension-grid">
      <div class="panel extension-editor">
        <h3>直接调用工具</h3>
        <p class="muted">
          不调用模型即可验证工具行为。此处仍经过与 Agent 相同的 schema、权限和
          hooks 检查。
        </p>
        <label
          >工具名<input
            v-model="toolName"
            list="tool-list"
            aria-label="工具名" /></label
        ><datalist id="tool-list">
          <option
            v-for="t in catalog.tools"
            :key="t.name"
            :value="t.name"
          /></datalist
        ><label
          >权限<select v-model="permission">
            <option value="read-only">只读工作区</option>
            <option value="workspace-write">允许写工作区</option>
            <option value="danger-full-access">
              允许执行（本机进程，无 OS 沙箱）
            </option>
          </select></label
        ><label
          >参数 JSON<textarea
            v-model="argumentsText"
            class="code-input"
            rows="9"
            aria-label="工具参数"
          ></textarea></label
        ><button
          class="btn primary"
          :disabled="busy"
          @click="
            perform(() =>
              api(
                '/tools/execute',
                json('POST', {
                  name: toolName,
                  arguments: JSON.parse(argumentsText),
                  permission,
                }),
              ),
            )
          "
        >
          <Play :size="16" />执行工具
        </button>
      </div>
      <div class="panel extension-card">
        <h3>当前工具定义</h3>
        <pre class="code-output">{{
          JSON.stringify(
            catalog.tools.find((t: any) => t.name === toolName) || {
              hint: "插件工具名：plugin__插件名__工具名",
            },
            null,
            2,
          )
        }}</pre>
      </div>
    </div>
    <div v-if="tab === 'catalog'" class="panel extension-editor">
      <div class="section-head">
        <h3>
          {{ catalog.tools.length }} 个工具 ·
          {{ catalog.commands.length }} 个原始命令
        </h3>
        <input
          v-model="filter"
          placeholder="搜索名称或说明"
          aria-label="搜索迁移目录"
        />
      </div>
      <p class="muted">
        原始 commit
        {{
          catalog.revision?.slice(0, 12)
        }}。命令以实际状态标注；原有占位项不会算作完成。
      </p>
      <div class="catalog-table">
        <table>
          <thead>
            <tr>
              <th>命令</th>
              <th>说明</th>
              <th>上游</th>
              <th>Python / Web</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in filtered(catalog.commands)" :key="c.name">
              <td>
                <code>/{{ c.name }}</code>
              </td>
              <td>{{ c.summary }}</td>
              <td>{{ c.upstream_implemented ? "已实现" : "占位" }}</td>
              <td>
                <span
                  class="status"
                  :class="
                    c.python_status !== 'upstream-placeholder'
                      ? 'completed'
                      : 'cancelled'
                  "
                  >{{
                    c.python_status === "python-adapted"
                      ? c.mode === "agent-prompt"
                        ? "Agent 工作流"
                        : "Python/Vue 适配"
                      : c.python_status === "python-extension"
                        ? "Python 扩展"
                        : "原有占位 · 未实现"
                  }}</span
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <section v-if="output" class="panel extension-editor result-panel">
      <div class="section-head">
        <h3>执行结果</h3>
        <button class="text-btn" @click="output = ''">清空</button>
      </div>
      <pre class="code-output" aria-live="polite">{{ output }}</pre>
    </section>
  </section>
</template>
