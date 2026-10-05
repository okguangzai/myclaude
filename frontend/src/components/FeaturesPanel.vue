<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, json, apiBase } from "../api";
import { installDesktop, download } from "../media";
import Markdown from "./Markdown.vue";
const emit = defineEmits(["navigate"]);
const state = ref<any>(),
  sessions = ref<any[]>([]),
  session = ref(""),
  permission = ref("workspace-write"),
  error = ref(""),
  result = ref<any>(),
  busy = ref(false),
  tab = ref("experience");
const tabs = [
  ["experience", "编辑与设备"],
  ["privacy", "隐私与限流"],
  ["collaboration", "分享与反馈"],
  ["automation", "宏与别名"],
  ["maintenance", "维护与集成"],
];
const command = ref("/help"),
  feedback = ref(""),
  macro = ref("学习工作流"),
  alias = ref(""),
  target = ref("/status"),
  path = ref("."),
  baseline = ref(""),
  count = ref(1),
  packName = ref("我的贴纸"),
  packItems = ref("✅,🐛,💡"),
  shell = ref("powershell");
async function refresh() {
  [state.value, sessions.value] = await Promise.all([
    api("/features"),
    api("/sessions"),
  ]);
}
async function action(fn: () => Promise<any>) {
  busy.value = true;
  error.value = "";
  try {
    await fn();
    await refresh();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function run(text: string) {
  await action(async () => {
    const value = await api(
      "/commands",
      json("POST", {
        text,
        session_id: session.value || null,
        permission: permission.value,
      }),
    );
    result.value = value;
    if (value.kind === "ui" && value.action === "desktop")
      await installDesktop();
  });
}
async function prefs(changes: any) {
  await action(() => api("/features/preferences", json("PUT", changes)));
}
async function enableNotifications() {
  await action(async () => {
    if (!("Notification" in window)) throw Error("此浏览器不支持桌面通知");
    const allowed = await Notification.requestPermission();
    if (allowed !== "granted")
      throw Error("浏览器尚未授权通知；站内通知仍可使用");
    await api(
      "/features/preferences",
      json("PUT", { desktop_notifications: true, notifications: true }),
    );
  });
}
function shareUrl(id: string) {
  return (
    location.origin +
    import.meta.env.BASE_URL +
    "?share=" +
    encodeURIComponent(id)
  );
}
async function copy(text: string) {
  await action(() => navigator.clipboard.writeText(text));
}
onMounted(() => action(refresh));
</script>
<template>
  <section class="features-page">
    <div class="section-heading">
      <div>
        <span class="eyebrow">WORKSPACE FEATURES</span>
        <h2>功能工作台</h2>
        <p class="muted">
          编辑体验、自动化和维护操作。设备访问在使用时由浏览器授权。
        </p>
      </div>
    </div>
    <div class="feature-tabs">
      <button
        v-for="[id, label] in tabs"
        :key="id"
        class="btn"
        :class="{ primary: tab === id }"
        @click="tab = id"
      >
        {{ label }}
      </button>
    </div>
    <div class="notice error" v-if="error" role="alert">{{ error }}</div>
    <div class="feature-context">
      <label
        >操作会话<select v-model="session">
          <option value="">未选择</option>
          <option v-for="s in sessions" :key="s.id" :value="s.id">
            {{ s.title }}
          </option>
        </select></label
      ><label
        >本次操作权限<select v-model="permission">
          <option value="read-only">只读</option>
          <option value="workspace-write">工作区写入</option>
          <option value="danger-full-access">允许本机执行</option>
        </select></label
      >
    </div>
    <template v-if="state">
      <div v-if="tab === 'experience'" class="feature-grid">
        <article class="feature-card">
          <h3>对话编辑器</h3>
          <label
            v-for="[key, label] in [
              ['vim', 'Vim 模式'],
              ['voice', '语音模式'],
              ['advisor', '顾问只读模式'],
              ['notifications', '运行结束通知'],
            ]"
            :key="key"
            ><input
              type="checkbox"
              :checked="state.preferences[key]"
              @change="
                prefs({ [key]: ($event.target as HTMLInputElement).checked })
              "
            />{{ label }}</label
          >
          <p class="muted">
            顾问模式在后端强制使用只读权限。语音输入可以在发送前修改。
          </p>
          <button class="btn primary" @click="emit('navigate', 'chat')">
            打开对话 · 图片 / 截图 / 语音
          </button>
        </article>
        <article class="feature-card">
          <h3>键盘快捷键</h3>
          <label v-for="name in ['send', 'new', 'stop', 'focus']" :key="name"
            >{{
              {
                send: "发送",
                new: "新建会话",
                stop: "停止",
                focus: "聚焦输入",
              }[name]
            }}<input
              v-model="state.preferences.keybindings[name]"
              :aria-label="'快捷键 ' + name"
          /></label>
          <p class="muted">
            格式：Enter、Ctrl+Enter、Alt+n、Escape；不可重复。
          </p>
          <div class="action-row">
            <button
              class="btn"
              @click="
                run(
                  '/keybindings ' +
                    JSON.stringify(state.preferences.keybindings),
                )
              "
            >
              保存快捷键</button
            ><button class="btn" @click="run('/keybindings reset')">
              恢复默认
            </button>
          </div>
        </article>
        <article class="feature-card">
          <h3>语音服务</h3>
          <p>
            ASR 配置：{{ state.speech.ASR ? "已配置" : "未配置" }} · TTS
            配置：{{ state.speech.TTS ? "已配置" : "未配置" }}
          </p>
          <p class="muted">
            凭据保存在服务端。配置存在不代表 Token 尚未过期；对话页可实际测试。
          </p>
          <p>图片模型：{{ state.vision_model || "沿用对话模型" }}</p>
          <button class="btn" @click="emit('navigate', 'settings')">
            模型设置
          </button>
        </article>
        <article class="feature-card">
          <h3>通知</h3>
          <button class="btn" @click="enableNotifications">
            授权浏览器通知</button
          ><button class="btn" @click="prefs({ desktop_notifications: false })">
            关闭桌面通知</button
          ><button
            class="btn"
            @click="
              action(() => api('/features/notifications/read', json('POST')))
            "
          >
            全部已读
          </button>
          <p v-for="n in state.notifications.slice(-8).reverse()" :key="n.id">
            {{ n.read ? "已读" : "未读" }} · {{ n.status }} · {{ n.session_id }}
          </p>
        </article>
        <article class="feature-card">
          <h3>贴纸包</h3>
          <label>名称<input v-model="packName" /></label
          ><label>贴纸（逗号分隔）<input v-model="packItems" /></label
          ><button
            class="btn"
            @click="
              run(
                '/stickers ' +
                  JSON.stringify({
                    name: packName,
                    items: packItems
                      .split(',')
                      .map((x) => x.trim())
                      .filter(Boolean),
                  }),
              )
            "
          >
            新增贴纸包
          </button>
          <p v-for="pack in state.stickers" :key="pack.id">
            {{ pack.name }} · {{ pack.items.join(" ") }}
            <button
              v-if="pack.id !== 'builtin'"
              class="btn"
              @click="run('/stickers remove ' + pack.id)"
            >
              删除
            </button>
          </p>
        </article>
      </div>
      <div v-if="tab === 'privacy'" class="feature-grid">
        <article class="feature-card">
          <h3>隐私设置</h3>
          <label
            ><input
              type="checkbox"
              v-model="state.privacy.store_history"
            />保存后续对话内容</label
          ><label
            ><input
              type="checkbox"
              v-model="state.privacy.store_traces"
            />保存详细运行轨迹</label
          ><label
            ><input
              type="checkbox"
              v-model="state.privacy.allow_cloud"
            />允许云模型与云语音调用</label
          >
          <p class="muted">
            关闭后仍保留运行状态和用量。既有历史、主动上传的附件不自动删除。云调用开关不控制用户明确执行的
            Web / Shell 工具；本机模型仍可调用。
          </p>
          <button
            class="btn primary"
            @click="run('/privacy-settings ' + JSON.stringify(state.privacy))"
          >
            保存隐私设置
          </button>
        </article>
        <article class="feature-card">
          <h3>模型与语音限流</h3>
          <label
            >每分钟请求数<input
              type="number"
              min="1"
              max="6000"
              v-model="state.rate_limit.rpm" /></label
          ><label
            >最大并发<input
              type="number"
              min="1"
              max="32"
              v-model="state.rate_limit.concurrency"
          /></label>
          <p class="muted">
            作用于单个应用进程；等待中的请求可以取消。模型重试计入额度。
          </p>
          <button
            class="btn primary"
            @click="
              run(
                `/rate-limit ${state.rate_limit.rpm} ${state.rate_limit.concurrency}`,
              )
            "
          >
            保存限流设置
          </button>
        </article>
      </div>
      <div v-if="tab === 'collaboration'" class="feature-grid">
        <article class="feature-card">
          <h3>会话分享</h3>
          <p class="muted">
            创建只读快照，24
            小时有效，仍需要网站登录。只包含文字对话，省略工具轨迹和图片。
          </p>
          <button
            class="btn primary"
            :disabled="!session"
            @click="run('/share create')"
          >
            分享选中会话
          </button>
          <div v-for="s in state.shares" :key="s.id" class="feature-item">
            <strong>{{ s.title }}</strong
            ><span>{{
              s.revoked
                ? "已撤销"
                : new Date(s.expires_at * 1000).toLocaleString()
            }}</span
            ><a
              v-if="!s.revoked"
              :href="shareUrl(s.id)"
              target="_blank"
              rel="noopener"
              >查看</a
            ><button
              v-if="!s.revoked"
              class="btn"
              @click="copy(shareUrl(s.id))"
            >
              复制链接</button
            ><button
              v-if="!s.revoked"
              class="btn"
              @click="run('/share revoke ' + s.id)"
            >
              撤销
            </button>
          </div>
        </article>
        <article class="feature-card">
          <h3>反馈记录</h3>
          <label
            >问题或建议<textarea
              v-model="feedback"
              maxlength="4000"
              rows="4"
            ></textarea>
          </label>
          <p class="muted">保存在本应用，不会发送给外部供应商。</p>
          <button
            class="btn"
            :disabled="!feedback.trim()"
            @click="run('/feedback ' + feedback)"
          >
            保存反馈
          </button>
          <div v-for="f in state.feedback" :key="f.id" class="feature-item">
            <p>{{ f.message }}</p>
            <span>{{ f.status }}</span
            ><button
              v-if="f.status === 'open'"
              class="btn"
              @click="
                action(() =>
                  api('/features/feedback/' + f.id + '/resolve', json('POST')),
                )
              "
            >
              标记解决
            </button>
          </div>
        </article>
      </div>
      <div v-if="tab === 'automation'" class="feature-grid">
        <article class="feature-card">
          <h3>命令别名</h3>
          <label>别名<input v-model="alias" placeholder="例如 inspect" /></label
          ><label>目标命令<input v-model="target" /></label
          ><button
            class="btn"
            @click="run('/alias set ' + alias + ' ' + target)"
          >
            保存别名
          </button>
          <p v-for="(value, key) in state.aliases" :key="key">
            /{{ key }} → {{ value }}
            <button class="btn" @click="run('/alias remove ' + key)">
              删除
            </button>
          </p>
        </article>
        <article class="feature-card">
          <h3>工作流宏</h3>
          <label>宏名称<input v-model="macro" /></label>
          <p class="muted">
            开始录制后，通过下方命令框或对应会话记录命令。重放遵守所选权限，不会自动提权。
          </p>
          <div class="action-row">
            <button class="btn" @click="run('/macro record ' + macro)">
              开始录制</button
            ><button class="btn" @click="run('/macro stop')">结束录制</button>
          </div>
          <div v-for="m in state.macros" :key="m.id" class="feature-item">
            <strong>{{ m.id }}</strong>
            <pre>{{ m.commands.join("\n") }}</pre>
            <button class="btn" @click="run('/macro run ' + m.id)">运行</button
            ><button class="btn" @click="run('/macro remove ' + m.id)">
              删除
            </button>
          </div>
        </article>
      </div>
      <div v-if="tab === 'maintenance'" class="feature-grid">
        <article class="feature-card">
          <h3>Python 自动修复</h3>
          <label>工作区相对路径<input v-model="path" /></label>
          <p class="muted">
            使用 Ruff 安全修复，先预览差异；应用前备份源文件。
          </p>
          <div class="action-row">
            <button
              class="btn"
              @click="run('/autofix preview ' + JSON.stringify(path))"
            >
              预览修复</button
            ><button
              class="btn"
              @click="run('/autofix apply ' + JSON.stringify(path))"
            >
              应用修复
            </button>
          </div>
        </article>
        <article class="feature-card">
          <h3>数据库迁移</h3>
          <p class="muted">检查版本与校验和，应用前生成 SQLite 备份。</p>
          <button class="btn" @click="run('/migrate plan')">查看迁移计划</button
          ><button class="btn" @click="run('/migrate apply')">
            备份并迁移
          </button>
        </article>
        <article class="feature-card">
          <h3>模型基准测试</h3>
          <label
            >轮数<input type="number" min="1" max="5" v-model="count"
          /></label>
          <p class="muted">真实调用当前模型，记录首字时间、耗时和接口用量。</p>
          <button class="btn" @click="run('/benchmark ' + count)">
            运行基准测试
          </button>
        </article>
        <article class="feature-card">
          <h3>版本与发布</h3>
          <label>工作区 Git 对比基线（可选）<input v-model="baseline" /></label>
          <div class="action-row">
            <button class="btn" @click="run('/changelog ' + baseline)">
              提交历史</button
            ><button class="btn" @click="run('/release-notes ' + baseline)">
              生成发布说明</button
            ><button class="btn" @click="run('/upgrade check')">
              检查升级
            </button>
          </div>
          <p class="muted">
            服务器更新由部署方管理。本机 CLI 支持 /upgrade apply
            快进升级。
          </p>
          <a
            v-if="state?.pipeline_url"
            :href="state.pipeline_url"
            target="_blank"
            rel="noopener"
            >查看发布流水线</a
          >
        </article>
        <article class="feature-card">
          <h3>终端集成</h3>
          <label
            >Shell<select v-model="shell">
              <option>powershell</option>
              <option>bash</option>
              <option>zsh</option>
              <option>fish</option>
            </select></label
          ><button class="btn" @click="run('/terminal-setup ' + shell)">
            生成启动与补全脚本</button
          ><button
            v-if="result?.result?.script"
            class="btn"
            @click="
              download(
                result.result.script,
                'myclaude.' + (shell === 'powershell' ? 'ps1' : shell),
              )
            "
          >
            下载脚本
          </button>
          <p class="muted">
            脚本使用应用所在机器的路径；本机使用请在本机 CLI 执行此命令。
          </p>
        </article>
        <article class="feature-card">
          <h3>桌面与 IDE</h3>
          <button class="btn primary" @click="action(installDesktop)">
            安装桌面入口
          </button>
          <p class="muted">
            支持安装的浏览器显示安装提示；其他浏览器下载 Windows 快捷方式。
          </p>
          <a :href="apiBase + '/features/ide/extension'" download
            >下载 VS Code / Cursor 扩展</a
          >
          <p>
            在编辑器选择「从 VSIX 安装」，运行 MyClaude: Configure
            connection，再选中代码运行 Ask about
            selection。密码保存在编辑器凭据存储中。
          </p>
          <button class="btn" @click="run('/ide vscode')">
            查看工作区连接信息
          </button>
        </article>
      </div>
    </template>
    <form class="feature-command" @submit.prevent="run(command)">
      <label>执行命令<input v-model="command" aria-label="执行命令" /></label
      ><button class="btn primary" :disabled="busy">
        {{ busy ? "执行中…" : "执行" }}
      </button>
    </form>
    <article v-if="result" class="feature-card">
      <h3>执行结果</h3>
      <Markdown v-if="result.result?.markdown" :text="result.result.markdown" />
      <pre v-else>{{ JSON.stringify(result.result ?? result, null, 2) }}</pre>
    </article>
  </section>
</template>
<style scoped>
.features-page {
  max-width: 1200px;
  margin: auto;
}
.feature-tabs,
.feature-context {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 20px 0;
}
.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
  gap: 18px;
}
.feature-card {
  padding: 24px;
  border: 1px solid #e2ded6;
  border-radius: 14px;
  background: var(--surface, #fff);
  overflow: hidden;
}
.feature-card h3 {
  margin-top: 0;
}
.feature-card label,
.feature-context label,
.feature-command label {
  display: flex;
  gap: 10px;
  margin: 12px 0;
  flex-wrap: wrap;
  align-items: center;
}
.feature-card input:not([type="checkbox"]),
.feature-card textarea,
.feature-command input {
  width: 100%;
  padding: 9px;
  border: 1px solid #ddd;
  border-radius: 6px;
}
.feature-card input[type="checkbox"] {
  width: 18px;
  height: 18px;
}
.feature-card .btn {
  margin: 4px;
}
.feature-card pre {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 500px;
  overflow: auto;
}
.feature-item {
  border-top: 1px solid #eee;
  margin-top: 14px;
  padding-top: 12px;
}
.feature-item span,
.feature-item a {
  margin: 0 8px;
}
.feature-command {
  display: flex;
  align-items: end;
  gap: 12px;
  margin: 24px 0;
}
.feature-command label {
  flex: 1;
}
.feature-command button {
  margin-bottom: 12px;
}
.feature-card p {
  line-height: 1.7;
}
</style>
