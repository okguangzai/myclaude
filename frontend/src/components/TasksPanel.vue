<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import {
  Play,
  Square,
  RefreshCw,
  Users,
  Clock,
  GitBranch,
} from "lucide-vue-next";
import { api, json } from "../api";
const mode = ref("agent"),
  prompt = ref("读取 README.txt，解释 Agent 循环的关键步骤。"),
  description = ref("学习任务");
const permission = ref("read-only"),
  schedule = ref("0 9 * * 1-5"),
  teamTasks = ref(
    "读取 README.txt，概括项目用途。\n读取 README.txt，给出学习建议。",
  );
const jobs = ref<any[]>([]),
  laneBoard = ref<any>({ active: [], blocked: [], finished: [] }),
  questions = ref<any[]>([]),
  answers = ref<Record<string, string>>({}),
  state = ref<any>({ teams: [], schedules: [], workers: [] }),
  detail = ref<any>(null),
  error = ref(""),
  busy = ref(false);
async function refresh() {
  [jobs.value, state.value, questions.value, laneBoard.value] =
    await Promise.all([
      api("/jobs"),
      api("/extensions"),
      api("/questions"),
      api("/runtime/lane-board"),
    ]);
  if (detail.value?.id)
    detail.value =
      jobs.value.find((j) => j.id === detail.value.id) || detail.value;
}
async function act(work: () => Promise<any>) {
  busy.value = true;
  error.value = "";
  try {
    detail.value = await work();
    await refresh();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function create() {
  if (mode.value === "agent")
    return api(
      "/tasks",
      json("POST", {
        prompt: prompt.value,
        description: description.value,
        permission: permission.value,
      }),
    );
  if (mode.value === "team")
    return api(
      "/teams",
      json("POST", {
        name: description.value,
        tasks: teamTasks.value.split("\n").filter((x) => x.trim()),
        permission: permission.value,
      }),
    );
  return api(
    "/schedules",
    json("POST", {
      schedule: schedule.value,
      prompt: prompt.value,
      description: description.value,
      permission: permission.value,
    }),
  );
}
let timer: ReturnType<typeof setInterval>;
onMounted(() => {
  refresh().catch((e) => (error.value = e.message));
  timer = setInterval(() => refresh().catch(() => {}), 2500);
});
onBeforeUnmount(() => clearInterval(timer));
</script>
<template>
  <section class="extension-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DELEGATE / OBSERVE / CONTROL</span>
        <h1>任务与协作<span>。</span></h1>
        <p>把工作交给子 Agent，在独立会话中执行，结果和过程留在本机。</p>
      </div>
      <button class="btn" @click="act(refresh)">
        <RefreshCw :size="16" />刷新
      </button>
    </div>
    <div v-if="error" class="notice error">{{ error }}</div>
    <div class="panel extension-editor">
      <div class="section-head">
        <h3>运行状态</h3>
        <span class="badge">每 10 秒心跳</span>
      </div>
      <p class="small muted">
        {{ laneBoard.active.length }} 个活动任务 ·
        {{ laneBoard.blocked.length }} 个阻塞任务 ·
        {{ laneBoard.finished.length }} 个已结束任务
      </p>
      <div
        v-for="lane in [...laneBoard.active, ...laneBoard.blocked]"
        :key="lane.task_id"
        class="job-row"
      >
        <span>{{ lane.prompt?.slice(0, 70) || lane.task_id }}</span
        ><span class="badge">{{
          {
            healthy: "心跳正常",
            stalled: "心跳超时",
            transport_dead: "连接已断",
            unknown: "等待心跳",
          }[lane.freshness as string] || lane.freshness
        }}</span>
      </div>
    </div>
    <div v-for="q in questions" :key="q.id" class="question-card">
      <strong>{{ q.question }}</strong>
      <p class="small muted">会话 {{ q.session_id }}</p>
      <div class="action-row">
        <button
          v-for="option in q.options"
          :key="option"
          class="btn"
          @click="
            act(() =>
              api(
                '/questions/' + q.id + '/answer',
                json('POST', { answer: option }),
              ),
            )
          "
        >
          {{ option }}</button
        ><input v-model="answers[q.id]" placeholder="输入回答" /><button
          class="btn primary"
          :disabled="!answers[q.id]?.trim()"
          @click="
            act(() =>
              api(
                '/questions/' + q.id + '/answer',
                json('POST', { answer: answers[q.id] }),
              ),
            )
          "
        >
          提交回答
        </button>
      </div>
    </div>
    <div class="extension-grid">
      <div class="panel extension-editor">
        <div class="extension-tabs compact">
          <button
            v-for="t in [
              { id: 'agent', label: '后台 Agent', icon: GitBranch },
              { id: 'team', label: '并行团队', icon: Users },
              { id: 'cron', label: '定时任务', icon: Clock },
            ]"
            :key="t.id"
            :class="{ active: mode === t.id }"
            @click="mode = t.id"
          >
            <component :is="t.icon" :size="16" />{{ t.label }}
          </button>
        </div>
        <label
          >任务名称<input v-model="description" aria-label="任务名称" /></label
        ><label v-if="mode === 'team'"
          >每行一个任务，最多四个<textarea
            v-model="teamTasks"
            rows="6"
            aria-label="团队任务"
          ></textarea></label
        ><label v-else
          >任务目标<textarea
            v-model="prompt"
            rows="6"
            aria-label="后台任务目标"
          ></textarea></label
        ><label
          >工具权限<select v-model="permission">
            <option value="read-only">只读工作区</option>
            <option value="workspace-write">允许写工作区</option>
            <option value="danger-full-access">允许执行（本机进程）</option>
          </select></label
        ><label v-if="mode === 'cron'"
          >Cron 表达式（UTC，五字段）<input v-model="schedule"
        /></label>
        <div class="notice">
          {{
            mode === "agent"
              ? "最多 4 个顶层任务并发。子任务深度最多 2 层，共享 40 次模型请求预算。"
              : "任务使用上方选择的工具权限。定时器只在本服务运行时触发，不补跑停机期间任务。"
          }}
        </div>
        <button class="btn primary" :disabled="busy" @click="act(create)">
          <Play :size="16" />{{ mode === "cron" ? "创建定时任务" : "开始任务" }}
        </button>
      </div>
      <div class="panel extension-editor">
        <div class="section-head">
          <h3>后台任务</h3>
          <span class="badge"
            >{{
              jobs.filter((j) => ["queued", "running"].includes(j.status))
                .length
            }}
            ACTIVE</span
          >
        </div>
        <div v-if="!jobs.length" class="empty-inline">
          还没有任务。提交一个目标，然后查看真实执行结果。
        </div>
        <div
          v-for="j in jobs"
          :key="j.id"
          class="job-row"
          :class="{ chosen: detail?.id === j.id }"
          @click="detail = j"
        >
          <div>
            <strong>{{ j.payload?.description || j.kind }}</strong>
            <p class="small muted">
              {{ j.created_at.slice(0, 19).replace("T", " ") }} · {{ j.kind }}
            </p>
          </div>
          <span class="status" :class="j.status">{{ j.status }}</span
          ><button
            v-if="['queued', 'running'].includes(j.status)"
            class="icon-btn"
            aria-label="停止任务"
            @click.stop="
              act(() => api('/jobs/' + j.id + '/stop', json('POST', {})))
            "
          >
            <Square :size="16" />
          </button>
        </div>
      </div>
    </div>
    <div class="extension-grid secondary-grid">
      <div class="panel extension-editor">
        <h3>团队</h3>
        <div v-for="t in state.teams" :key="t.id" class="job-row">
          <div>
            <strong>{{ t.name }}</strong>
            <p class="small muted">
              {{ t.tasks.length }} 个任务 · 各自保存执行记录
            </p>
          </div>
          <button
            class="text-btn"
            @click="
              act(() =>
                api(
                  '/tools/execute',
                  json('POST', {
                    name: 'TeamDelete',
                    arguments: { team_id: t.id },
                    permission: 'danger-full-access',
                  }),
                ),
              )
            "
          >
            结束团队
          </button>
        </div>
        <p v-if="!state.teams.length" class="muted">暂无团队</p>
      </div>
      <div class="panel extension-editor">
        <h3>定时任务</h3>
        <div v-for="c in state.schedules" :key="c.id" class="job-row">
          <div>
            <strong>{{ c.description || c.prompt.slice(0, 30) }}</strong>
            <p class="small muted">
              {{ c.schedule }} UTC · 下次
              {{ new Date(c.next_run * 1000).toLocaleString() }}
            </p>
          </div>
          <button
            class="text-btn"
            @click="
              act(() =>
                api(
                  '/tools/execute',
                  json('POST', {
                    name: 'CronDelete',
                    arguments: { cron_id: c.id },
                    permission: 'danger-full-access',
                  }),
                ),
              )
            "
          >
            删除
          </button>
        </div>
        <p v-if="!state.schedules.length" class="muted">暂无定时任务</p>
      </div>
    </div>
    <section v-if="detail" class="panel extension-editor result-panel">
      <div class="section-head">
        <h3>任务详情</h3>
        <span class="badge">持久化记录</span>
      </div>
      <pre class="code-output">{{ JSON.stringify(detail, null, 2) }}</pre>
    </section>
  </section>
</template>
