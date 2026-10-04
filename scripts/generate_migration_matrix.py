"""Generate a reviewable migration inventory from the frozen upstream catalog."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.commands import PROMPTS, UNSUPPORTED  # noqa: E402
from app.extensions import CATALOG  # noqa: E402

notes = {
    "bash": "真实 Bash 进程；Windows 优先 Git Bash；Linux 可探测 namespace/network，文件系统隔离请求拒绝",
    "PowerShell": "真实本机 PowerShell；支持后台任务、超时与取消",
    "REPL": "Python/JavaScript/Bash 独立进程，不保留跨调用变量；exitCode 与 durationMs",
    "read_file": "工作区 UTF-8、offset/limit；256 KB 文件、60000 字符结果上限",
    "write_file": "原子替换工作区文件；256 KB 写入上限",
    "edit_file": "唯一匹配或 replace_all；拒绝空 old_string",
    "glob_search": "最多扫描 5000 文件、返回 300 项；含花括号展开与原 filenames/numFiles 字段",
    "grep_search": "regex 超时保护、类型/glob/上下文/输出模式；最多 300 项",
    "WebFetch": "HTTP(S) 公网内容提取；1 MB 上限；不自动跟随重定向",
    "WebSearch": "公共搜索 HTML 适配，返回链接并执行域名过滤；失败明确报错",
    "RemoteTrigger": "真实 HTTP 请求；仅公网；校验并固定 DNS 地址",
    "TodoWrite": "按会话保存 Todo；oldTodos/newTodos，全完成时清空及验证提醒",
    "Skill": "读取用户/工作区分层技能，YAML frontmatter、辅助文件安装与参数",
    "Agent": "独立子会话，等待真实结果，继承权限和树预算；按子类型限制工具并采用 subagentModel",
    "ToolSearch": "select: 名称选择、+必选词、原版评分排序；仅返回当前授权 schema",
    "NotebookEdit": "按 cell id 插入/替换/删除，代码 cell 清空旧输出",
    "Sleep": "可取消等待，单次最多 60 秒",
    "SendUserMessage": "保存本地通知；附件必须是工作区路径，不对外发送消息",
    "Config": "原偏好字段、model、permissions.defaultMode 持久化；4 个 Python 运行参数只作用本轮；不访问密钥、不提升本轮授权",
    "EnterPlanMode": "本轮有效权限收窄为只读",
    "ExitPlanMode": "仅恢复本轮原先授予的权限",
    "StructuredOutput": "保存 JSON 对象；原 schema 允许任意对象，无额外业务 schema",
    "AskUserQuestion": "等待页面/API 回答，超时或取消移除待答请求",
    "TaskCreate": "立即返回后台 Agent job_id",
    "RunTaskPacket": "校验并保存完整合同；扩展 live schema 支持 scope_path 等运行时字段；repo 必须为当前工作区；不自动强制 Git 策略",
    "TaskGet": "按 ID 读取持久化任务",
    "TaskList": "列出最近 200 个任务",
    "TaskOutput": "读取真实状态、结果和关联会话/运行",
    "TaskStop": "实际取消任务与拥有的子进程，传播到运行中的子任务",
    "TaskUpdate": "运行中追加消息入队，在安全轮次继续；结束后在原会话创建后续任务",
    "TeamCreate": "1–4 个任务并行；共享工作区，独立会话",
    "TeamDelete": "取消团队任务并移除团队记录，保留任务历史",
    "CronCreate": "持久化 UTC 五字段 cron；服务运行期间触发",
    "CronList": "读取定时计划、执行次数、创建/更新时间、上次执行时间和 job_id",
    "CronDelete": "删除后续调度，不隐式取消已发出的独立任务",
    "LSP": "真实 Python/pylsp；可配置其它语言服务器；七类操作；无 path 时返回当前工作区缓存诊断（附观察时间）",
    "ListMcpResources": "查询已启用且授权的 MCP 服务资源列表",
    "ReadMcpResource": "真实 MCP resources/read",
    "McpAuth": "连接和环境凭据状态；没有 OAuth 浏览器授权向导",
    "MCP": "真实 tools/call；兼容通用入口；另有动态命名工具",
    "TestingPermission": "检查调用是否获执行权限，不执行 action 字符串",
}
for name in ("GitStatus", "GitDiff", "GitLog", "GitShow", "GitBlame"):
    notes[name] = "真实 Git 查询；校验 revision/路径，禁用外部 diff/textconv/pager"
for spec in CATALOG["tools"]:
    if spec["name"].startswith("Worker"):
        notes[spec["name"]] = (
            "持久会话 Worker；79 组原 Rust 状态/类型事件对照通过，含错目录/错任务/产物回执/重放/超时；文件观察接口、并发互斥和实际取消已验证；原版也是控制状态机；Python 额外启动持久 Agent 任务"
        )

lines = [
    "# 逐项迁移矩阵",
    "",
    f"上游基线：`{CATALOG['revision']}`。由 `scripts/generate_migration_matrix.py` 生成。",
    "",
    "0.8 已补齐源码审计缺口并实现原 25 个占位命令；55 个工具均有 Python 执行路径。冻结 catalog 保留原定义；运行时补充原 schema 遗漏的字段。接口和预算的有意适配、新增功能及验收证据见《迁移收口清单》；不宣称字节级等价。",
    "",
    "## 工具",
    "",
    "| 工具 | 上游最低权限 | Python 行为 / 差异 |",
    "| --- | --- | --- |",
]
for spec in CATALOG["tools"]:
    assert spec["name"] in notes, spec["name"]
    lines.append(f"| `{spec['name']}` | `{spec['required_permission']}` | {notes[spec['name']]} |")
lines += [
    "",
    "另保留 Python 版 list_files、search_files、retrieve_context，并动态注册 MCP/插件工具。",
    "",
    "## 命令",
    "",
    "上游 140 个命令中 36 个标记已实现。Python/Web 的 140 个入口均有实现或交互适配，其中 17 个为 Agent 提示工作流；0.8 新增原 25 个占位项的实际功能，见《完整功能使用与验收》。Web 适配不等同于原 TUI 的全部参数与交互。",
    "",
    "| 命令 | 上游 | Python/Web | 原有说明 |",
    "| --- | --- | --- | --- |",
]
for spec in CATALOG["commands"]:
    name = spec["name"]
    state = (
        "未实现（原有占位）"
        if name in UNSUPPORTED
        else "Agent 提示工作流"
        if name in PROMPTS
        else "Python/Vue 适配"
        if spec["implemented"]
        else "Python 扩展"
    )
    summary = spec["summary"].replace("|", "/").replace("\n", " ")
    alias = " / " + ", ".join(spec["aliases"]) if spec["aliases"] else ""
    lines.append(
        f"| `/{name}`{alias} | {'已实现' if spec['implemented'] else '占位'} | {state} | {summary} |"
    )
lines += [
    "",
    "## 兼容性说明",
    "",
    "CLI 独立运行。`/mcp`、`/plugin` 等带子命令时执行操作；Web 无参数时打开管理页。`/effort`、`/reasoning` 设置 reasoning_effort，`/fast` 调整轮数。`/compact` 生成摘要，`/undo` 仅撤回上下文；`/cost` 不虚构金额，运行时诊断支持显式单价。`/commit` 提交已暂存变更，`/pr`、`/issue` 依赖已登录的 gh。",
    "",
    "详细用法、权限边界、依赖条件和真实验证结果见《扩展开发与学习》和《迁移与验收》。",
]
(ROOT / "docs/迁移矩阵.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Generated {len(CATALOG['tools'])} tools and {len(CATALOG['commands'])} commands.")
