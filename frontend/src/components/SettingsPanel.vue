<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  Save,
  Cable,
  CheckCircle2,
  LoaderCircle,
  Eye,
  EyeOff,
} from "lucide-vue-next";
import { api, json } from "../api";
const emit = defineEmits(["changed"]);
const fallbackText = ref(""),
  routesText = ref("{}"),
  rulesMode = ref("auto"),
  rulesText = ref("");
const pluginRoots = ref("");
const form = ref<any>(null),
  tab = ref("model"),
  busy = ref(""),
  notice = ref(""),
  failure = ref(false),
  showKey = ref(false);
async function load() {
  form.value = await api("/settings");
  form.value.api_key = "";
  form.value.embedding_api_key = "";
  form.value.qdrant_api_key = "";
  fallbackText.value = (form.value.fallback_models || []).join("\n");
  routesText.value = JSON.stringify(form.value.provider_routes || {}, null, 2);
  pluginRoots.value = (form.value.plugin_external_directories || []).join("\n");
  rulesMode.value = Array.isArray(form.value.rules_import)
    ? "selected"
    : form.value.rules_import || "auto";
  rulesText.value = Array.isArray(form.value.rules_import)
    ? form.value.rules_import.join(", ")
    : "";
}
async function save() {
  busy.value = "save";
  notice.value = "";
  try {
    const values = { ...form.value };
    values.fallback_primary = values.fallback_primary?.trim() || null;
    values.subagent_model = values.subagent_model?.trim() || null;
    values.vision_model = values.vision_model?.trim() || null;
    values.plugin_external_directories = pluginRoots.value
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    values.plugin_bundled_root = values.plugin_bundled_root?.trim() || null;
    values.fallback_models = fallbackText.value
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      values.provider_routes = JSON.parse(routesText.value);
    } catch {
      throw new Error("模型连接配置必须是合法 JSON。");
    }
    values.rules_import =
      rulesMode.value === "selected"
        ? rulesText.value
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
        : rulesMode.value;
    Object.keys(values)
      .filter(
        (k) =>
          k.endsWith("_set") ||
          ["imported_mcp", "runtime_environment_names"].includes(k),
      )
      .forEach((k) => delete values[k]);
    await api("/settings", json("PUT", values));
    await load();
    notice.value =
      "配置已保存。模型参数下一次调用生效；修改插件存储路径后请重启服务。";
    failure.value = false;
    emit("changed");
  } catch (e: any) {
    notice.value = e.message;
    failure.value = true;
  } finally {
    busy.value = "";
  }
}
async function test(kind: string) {
  busy.value = kind;
  notice.value = "";
  try {
    const result = await api("/connections/" + kind, json("POST"));
    notice.value =
      kind === "embedding"
        ? `连接成功 · ${result.model} · ${result.dimension} 维`
        : kind === "qdrant"
          ? `Qdrant 鉴权成功 · 集合 ${result.collection}`
          : `模型连接成功 · ${result.model} · ${result.reply}`;
    failure.value = false;
  } catch (e: any) {
    notice.value = e.message;
    failure.value = true;
  } finally {
    busy.value = "";
  }
}
onMounted(() =>
  load().catch((e) => {
    notice.value = e.message;
    failure.value = true;
  }),
);
</script>
<template>
  <div class="page-intro">
    <div>
      <span class="eyebrow">CONTROL CENTER</span>
      <h2>你的模型，你的工作方式。</h2>
      <p>配置一次，让对话、工具与知识库协同工作。</p>
    </div>
    <button class="btn primary" :disabled="!!busy || !form" @click="save">
      <Save :size="16" />{{ busy === "save" ? "保存中…" : "保存配置" }}
    </button>
  </div>
  <div class="tabs">
    <button
      v-for="t in [
        { id: 'model', name: '对话模型' },
        { id: 'agent', name: 'Agent 与权限' },
        { id: 'rag', name: '知识库与连接' },
      ]"
      :class="{ active: tab === t.id }"
      @click="tab = t.id"
    >
      {{ t.name }}
    </button>
  </div>
  <div v-if="notice" class="notice" :class="{ error: failure }">
    {{ notice }}
  </div>
  <div v-if="form" class="settings-layout">
    <section class="panel form-panel" v-if="tab === 'model'">
      <div class="section-head">
        <h3>模型服务</h3>
        <span class="badge"
          ><CheckCircle2 :size="12" />{{
            form.api_key_set ? "已保存密钥" : "等待配置"
          }}</span
        >
      </div>
      <div class="form-grid">
        <label
          >接口协议<select v-model="form.provider">
            <option value="openai">OpenAI Compatible</option>
            <option value="anthropic">Anthropic Messages</option>
          </select></label
        ><label
          >模型 ID<input v-model="form.model" placeholder="qwen-flash"
        /></label>
      </div>
      <label
        >子 Agent 默认模型（可选）<input
          v-model="form.subagent_model"
          placeholder="留空继承对话模型"
        /><small
          >子任务可指定模型；留空时共用当前连接和对话模型，无需额外 Key。</small
        ></label
      >
      <label
        >图片模型（留空沿用对话模型）<input
          v-model="form.vision_model"
          placeholder="例如 qwen3-vl-flash；图片请求单独使用"
      /></label>
      <label
        >语音通道<select v-model="form.speech_provider">
          <option value="auto">自动：NLS 不可用时使用百炼</option>
          <option value="nls">NLS</option>
          <option value="dashscope">百炼</option>
        </select></label
      >
      <label>语音识别模型<input v-model="form.asr_model" /></label>
      <label>语音合成模型<input v-model="form.tts_model" /></label>
      <label>百炼发音人<input v-model="form.tts_voice" /></label>
      <label
        >API 基础地址<input
          v-model="form.base_url"
          placeholder="https://…/v1"
        /><small
          >填写基础地址即可，系统会移除末尾的 /chat/completions。</small
        ></label
      >
      <label
        >API Key
        <div class="secret-input">
          <input
            v-model="form.api_key"
            :type="showKey ? 'text' : 'password'"
            autocomplete="new-password"
            :placeholder="
              form.api_key_set ? '已配置 · 留空保留原密钥' : '输入你的 API Key'
            "
          /><button
            @click="showKey = !showKey"
            :aria-label="showKey ? '隐藏密钥' : '显示密钥'"
          >
            <EyeOff v-if="showKey" :size="16" /><Eye v-else :size="16" />
          </button></div
      ></label>
      <div class="form-grid">
        <label
          >最大输出 tokens<input
            type="number"
            v-model.number="form.max_tokens"
            min="64"
            max="32768" /></label
        ><label
          >Temperature<input
            type="number"
            v-model.number="form.temperature"
            min="0"
            max="2"
            step="0.1"
        /></label>
      </div>
      <div class="form-footer">
        <span>先保存，再测试当前配置。测试会调用云模型。</span
        ><button class="btn" :disabled="!!busy" @click="test('llm')">
          <LoaderCircle v-if="busy === 'llm'" class="spin" :size="15" /><Cable
            v-else
            :size="15"
          />测试模型
        </button>
      </div>
      <details class="advanced-settings">
        <summary>模型故障切换</summary>
        <p>
          主模型出现可重试连接错误时，按顺序尝试备用模型。已开始输出的响应不会重放。
        </p>
        <label
          >优先模型<input
            v-model="form.fallback_primary"
            placeholder="留空使用上方模型 ID"
        /></label>
        <label
          >备用模型（每行一个，最多 5 个）<textarea
            v-model="fallbackText"
            rows="3"
            placeholder="backup-model"
          />
        </label>
        <label
          >模型连接（JSON）<textarea
            v-model="routesText"
            rows="8"
            spellcheck="false"
          />
        </label>
        <small
          >以模型 ID 为键；每项填写 provider、base_url、api_key_env，可选
          model。api_key_env 引用 .env
          中的变量名，密钥仍保留在后端。未指定连接的模型沿用主连接。</small
        >
        <pre>
{ "backup-model": { "provider": "openai", "base_url": "https://example.com/v1", "api_key_env": "BACKUP_API_KEY", "model": "actual-model-id" } }</pre
        >
      </details>
    </section>
    <section class="panel form-panel" v-if="tab === 'agent'">
      <div class="section-head">
        <h3>执行边界</h3>
        <span class="badge">每次对话默认只读</span>
      </div>
      <label
        >工作区目录<input v-model="form.workspace" /><small
          >必须是已存在的文件夹。文件工具只在该目录内工作，凭据文件不可读取。</small
        ></label
      >
      <div class="form-grid">
        <label
          >最大 Agent 轮数<input
            type="number"
            v-model.number="form.max_turns"
            min="1"
            max="30" /></label
        ><label
          >运行超时（秒）<input
            type="number"
            v-model.number="form.timeout_seconds"
            min="10"
            max="900"
        /></label>
      </div>
      <label
        >上下文字符预算<input
          type="number"
          v-model.number="form.context_chars"
          min="5000"
          max="200000"
        /><small
          >超出时移除较早的完整轮次，保留工具调用与结果的配对。历史记录不会被删除。</small
        ></label
      >
      <label
        >系统提示词<textarea v-model="form.system_prompt" rows="6"></textarea>
      </label>
      <label
        >导入其他开发工具规则<select v-model="rulesMode">
          <option value="auto">自动发现</option>
          <option value="none">不导入</option>
          <option value="selected">选择来源</option>
        </select></label
      >
      <label v-if="rulesMode === 'selected'"
        >规则来源<input
          v-model="rulesText"
          placeholder="cursor, copilot, windsurf, plandex, crush"
      /></label>
      <small
        >项目自身的 AGENTS.md、CLAUDE.md 等仍会加载；按 Git
        根目录到当前目录收集，并限制总长度。运行记录中可查看实际载入的文件。</small
      >
      <label class="inline-check"
        ><input type="checkbox" v-model="form.telemetry_enabled" />保存本机
        JSONL 运行追踪</label
      >
      <small
        >追踪文件保存在
        .local/traces；包含调用事件和使用量，保存前屏蔽已知密钥。</small
      >
      <details class="advanced-settings">
        <summary>插件发现与模型连接</summary>
        <label
          >插件安装目录<input
            v-model="form.plugin_install_root"
            placeholder="留空使用数据目录下的 plugins"
        /></label>
        <label
          >兼容插件注册表<input
            v-model="form.plugin_registry_path"
            placeholder="可选：installed.json 的路径"
        /></label>
        <small
          >相对路径以工作目录为基准；改变存储位置后需重启服务。注册表导入不会自动启用插件。</small
        >
        <label
          >外部插件目录（每行一个）<textarea v-model="pluginRoots" rows="3" />
        </label>
        <label
          >Bundled 插件目录<input
            v-model="form.plugin_bundled_root"
            placeholder="留空使用内置示例插件"
        /></label>
        <small>保存后可在扩展页面同步目录。新发现的外部插件默认禁用。</small>
        <label
          >模型连接超时（秒）<input
            type="number"
            min="1"
            max="300"
            v-model.number="form.connect_timeout_seconds"
        /></label>
      </details>
    </section>
    <section class="panel form-panel" v-if="tab === 'rag'">
      <div class="section-head">
        <h3>向量模型</h3>
        <label class="inline-check"
          ><input type="checkbox" v-model="form.rag_enabled" />启用 RAG
          工具</label
        >
      </div>
      <div class="form-grid">
        <label>Embedding 模型<input v-model="form.embedding_model" /></label
        ><label
          >Embedding API Key<input
            type="password"
            v-model="form.embedding_api_key"
            :placeholder="
              form.embedding_api_key_set
                ? '已配置 · 留空保留'
                : '未配置时使用对话密钥'
            "
            autocomplete="new-password"
        /></label>
      </div>
      <label
        >Embedding 基础地址<input v-model="form.embedding_base_url" /></label
      ><button class="btn" :disabled="!!busy" @click="test('embedding')">
        <Cable :size="15" />{{
          busy === "embedding" ? "测试中…" : "测试向量模型"
        }}
      </button>
      <div class="form-divider"></div>
      <h3>Qdrant 数据库</h3>
      <div class="form-grid">
        <label>HTTP 地址<input v-model="form.qdrant_url" /></label
        ><label>独立集合名称<input v-model="form.qdrant_collection" /></label>
      </div>
      <label
        >Qdrant API Key<input
          type="password"
          v-model="form.qdrant_api_key"
          :placeholder="
            form.qdrant_api_key_set
              ? '已配置 · 留空保留'
              : '输入 Qdrant API Key'
          "
          autocomplete="new-password"
      /></label>
      <label class="inline-check"
        ><input type="checkbox" v-model="form.ssh_enabled" />通过 SSH
        隧道连接远程 Qdrant</label
      >
      <div v-if="form.ssh_enabled" class="form-grid">
        <label>SSH 用户@主机<input v-model="form.ssh_host" /></label
        ><label
          >SSH 端口<input type="number" v-model.number="form.ssh_port" /></label
        ><label>Qdrant 集群地址<input v-model="form.ssh_remote_host" /></label
        ><label
          >远程 HTTP 端口<input
            type="number"
            v-model.number="form.ssh_remote_port" /></label
        ><label
          >本机转发端口<input
            type="number"
            v-model.number="form.ssh_local_port"
        /></label>
      </div>
      <div class="form-footer">
        <span>SSH 使用本机已配置的密钥与 known_hosts。</span
        ><button class="btn" :disabled="!!busy" @click="test('qdrant')">
          <Cable :size="15" />{{
            busy === "qdrant" ? "连接中…" : "测试 Qdrant"
          }}
        </button>
      </div>
    </section>
    <aside class="settings-help">
      <span class="eyebrow"
        >FIELD NOTES / 0{{
          tab === "model" ? 1 : tab === "agent" ? 2 : 3
        }}</span
      >
      <h3>
        {{
          tab === "model"
            ? "一个清晰的模型入口"
            : tab === "agent"
              ? "给 Agent 明确的边界"
              : "让回答连接你的知识"
        }}
      </h3>
      <p>
        {{
          tab === "model"
            ? "对话模型负责理解任务与选择工具。支持百炼等 OpenAI 兼容接口，也保留 Anthropic 协议适配。"
            : tab === "agent"
              ? "Agent 不是一次模型请求。它会重复思考、调用工具、读取结果，因此需要轮数、时间与工作区限制。"
              : "Embedding 将文本变成向量，Qdrant 负责相似度检索。更换向量模型后请重新上传文档，并使用新的独立集合。"
        }}
      </p>
      <div class="help-rule"></div>
      <strong>配置保存在本机</strong>
      <p>
        密钥不会从配置接口回传浏览器。留空表示保留已保存的值，输入新值可以替换。
      </p>
      <span class="small muted">.local/settings.json</span>
    </aside>
  </div>
</template>
