"""User-facing commands with persistent behavior, separate from model tool dispatch."""

from __future__ import annotations

import json
import re
import secrets
import shlex
import sys
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from .db import now
from .execution import require
from .feature_ops import autofix, migrate, release_notes, upgrade
from .tools import safe_path

FEATURE_COMMANDS = frozenset(
    {
        "vim",
        "voice",
        "upgrade",
        "share",
        "feedback",
        "desktop",
        "ide",
        "advisor",
        "stickers",
        "release-notes",
        "keybindings",
        "privacy-settings",
        "terminal-setup",
        "listen",
        "speak",
        "changelog",
        "benchmark",
        "migrate",
        "autofix",
        "macro",
        "alias",
        "rate-limit",
        "notifications",
        "screenshot",
        "image",
    }
)
DEFAULT_KEYS = {"send": "Enter", "new": "Alt+n", "stop": "Escape", "focus": "Alt+i"}
DEFAULT_PREFS = {
    "vim": False,
    "voice": False,
    "advisor": False,
    "notifications": True,
    "keybindings": DEFAULT_KEYS,
    "desktop_notifications": False,
}
BUILTIN_STICKERS = [
    {"id": "builtin", "name": "开发日常", "items": ["✅", "🐛", "💡", "🚀", "🔍", "🧪", "📝", "🎯"]}
]


class Features:
    def __init__(self, ext):
        self.ext, self.jobs, self.store = ext, ext.jobs, ext.store

    def prefs(self):
        return {
            **deepcopy(DEFAULT_PREFS),
            **(self.jobs.record("features", "preferences") or {}).get("value", {}),
        }

    def preferences(self, changes):
        prefs = self.prefs()
        for name, value in changes.items():
            if name not in DEFAULT_PREFS:
                raise ValueError("未知偏好设置")
            if name == "keybindings":
                if not isinstance(value, dict) or set(value) != set(DEFAULT_KEYS):
                    raise ValueError("快捷键必须包含 send/new/stop/focus")
                if len(set(value.values())) != len(value):
                    raise ValueError("快捷键不能重复")
                if any(
                    not isinstance(v, str)
                    or not re.fullmatch(
                        r"(?:(?:Ctrl|Alt|Shift|Meta)\+)*(?:Enter|Escape|[a-z0-9])", v
                    )
                    for v in value.values()
                ):
                    raise ValueError("快捷键格式示例 Ctrl+Enter、Alt+n、Escape")
            elif type(value) is not bool:
                raise ValueError("偏好设置必须是布尔值")
            prefs[name] = value
        self.jobs.put("features", "preferences", {"value": prefs})
        return prefs

    def aliases(self):
        return {r["id"]: r["command"] for r in self.jobs.records("command_alias")}

    def expand(self, text):
        text = text.strip()
        aliases, seen = self.aliases(), set()
        for _ in range(9):
            name, _, rest = text.lstrip("/").partition(" ")
            if name not in aliases:
                return text
            if name in seen:
                raise ValueError("命令别名循环")
            seen.add(name)
            text = aliases[name] + (" " + rest if rest else "")
        raise ValueError("命令别名嵌套超过 8 层")

    def record_command(self, text, session_id):
        key = session_id or "local"
        active = self.jobs.record("macro_recording", key)
        if active:
            macro = self.jobs.record("macro", active["name"])
            if len(macro["commands"]) >= 32:
                raise ValueError("宏最多记录 32 条命令；请先 /macro stop")
            self.jobs.put(
                "macro", macro["id"], {"commands": [*macro["commands"], text], "updated_at": now()}
            )

    def validate_recording(self, text, session_id):
        active = self.jobs.record("macro_recording", session_id or "local")
        if not active:
            return
        command, _, argument = text.strip().lstrip("/").partition(" ")
        if command == "macro":
            return
        row = self.jobs.record("macro", active["name"])
        if not row or len(row["commands"]) >= 32:
            raise ValueError("宏已删除或达到 32 步上限；请先 /macro stop")
        if command in {"screenshot", "desktop", "ide"} or (
            command in {"image", "listen", "stickers"} and not argument
        ):
            raise ValueError(
                "设备选择、截图和交互式贴纸需要人工操作，不能录入自动宏；请先 /macro stop"
            )

    def shares(self):
        return [{k: v for k, v in r.items() if k != "snapshot"} for r in self.jobs.records("share")]

    def share(self, session_id, hours=24):
        session = self.ext.db.session(session_id)
        if not session:
            raise ValueError("请先选择一个会话")
        if not 1 <= hours <= 168:
            raise ValueError("分享有效期为 1–168 小时")
        messages = []
        for m in session["messages"]:
            if m["role"] in {"user", "assistant"} and m.get("content"):
                content = (
                    m["content"]
                    if isinstance(m["content"], str)
                    else "\n".join(b.get("text", "[图片已省略]") for b in m["content"])
                )
                content = self.store.redact(content)
                content = re.sub(
                    r"(?i)(api[_-]?key|password|authorization|token)(\s*[:=]\s*)(\S+)",
                    r"\1\2[REDACTED]",
                    content,
                )
                messages.append({"role": m["role"], "content": content})
        token = secrets.token_urlsafe(24)
        row = self.jobs.put(
            "share",
            token,
            {
                "session_id": session_id,
                "title": self.store.redact(session["title"]),
                "created_at": now(),
                "expires_at": time.time() + hours * 3600,
                "revoked": False,
                "snapshot": messages,
                "access": "same-site-authentication",
            },
        )
        return {k: v for k, v in row.items() if k != "snapshot"} | {"path": "?share=" + token}

    def read_share(self, token):
        row = self.jobs.record("share", token)
        if not row or row["revoked"] or row["expires_at"] <= time.time():
            raise ValueError("分享不存在、已撤销或已过期")
        return {
            "title": row["title"],
            "messages": row["snapshot"],
            "expires_at": row["expires_at"],
            "access": row["access"],
        }

    def revoke_share(self, token):
        row = self.jobs.record("share", token)
        if not row:
            raise ValueError("分享不存在")
        row.pop("id")
        row["revoked"] = True
        self.jobs.put("share", token, row)
        return {"id": token, "revoked": True}

    def feedback(self, message, session_id=None):
        if not message.strip() or len(message) > 4000:
            raise ValueError("反馈应为 1–4000 个字符")
        return self.jobs.put(
            "feedback",
            secrets.token_hex(12),
            {
                "message": self.store.redact(message.strip()),
                "session_id": session_id,
                "created_at": now(),
                "status": "open",
            },
        )

    def stickers(self):
        return BUILTIN_STICKERS + self.jobs.records("sticker_pack")

    def save_stickers(self, value):
        name, items = value.get("name"), value.get("items")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 60
            or not isinstance(items, list)
            or not 1 <= len(items) <= 64
        ):
            raise ValueError("贴纸包需要名称及 1–64 个文本/Emoji 贴纸")
        if any(not isinstance(x, str) or not x.strip() or len(x) > 32 for x in items):
            raise ValueError("每张文本贴纸应为 1–32 个字符")
        return self.jobs.put("sticker_pack", secrets.token_hex(8), {"name": name, "items": items})

    async def benchmark(self, count=1):
        if not 1 <= count <= 5:
            raise ValueError("基准测试轮数为 1–5")
        if not self.ext.jobs.agent:
            raise ValueError("模型执行器未启动")
        cfg = self.store.current.model_copy(update={"max_tokens": 32, "provider_retries": 0})
        rows = []
        for _ in range(count):
            start, first, usage, status = time.perf_counter(), None, {}, "completed"
            error = None
            try:
                async for event in self.ext.jobs.agent.provider.stream(
                    cfg, [{"role": "user", "content": "Reply only OK."}], [], "Be concise."
                ):
                    if event["type"] == "delta" and first is None:
                        first = time.perf_counter() - start
                    if event["type"] == "complete":
                        usage = event.get("usage", {})
            except Exception as exc:
                status, error = "failed", self.store.redact(str(exc))[:300]
            elapsed = time.perf_counter() - start
            rows.append(
                {
                    "status": status,
                    "error": error,
                    "first_token_seconds": first,
                    "duration_seconds": elapsed,
                    "usage": usage,
                    "output_tokens_per_second": usage.get("output_tokens", 0) / elapsed
                    if elapsed
                    else None,
                }
            )
        return self.jobs.put(
            "benchmark",
            secrets.token_hex(8),
            {
                "model": cfg.model,
                "created_at": now(),
                "samples": rows,
                "successful": sum(r["status"] == "completed" for r in rows),
            },
        )

    def terminal(self, shell="powershell"):
        from .cli import parser
        from .completions import generate

        if shell not in {"powershell", "bash", "zsh", "fish"}:
            raise ValueError("支持 powershell/bash/zsh/fish")
        python = str(Path(sys.executable)).replace("'", "''")
        backend = str(self.store.root / "backend").replace("'", "''")
        if shell == "powershell":
            launcher = f"function myclaude {{ $old = $env:PYTHONPATH; try {{ $env:PYTHONPATH = '{backend}'; & '{python}' -m app.cli @args }} finally {{ $env:PYTHONPATH = $old }} }}\n"
            suffix = "ps1"
        elif shell == "fish":
            launcher = f"function myclaude\n env PYTHONPATH={shlex.quote(str(self.store.root / 'backend'))} {shlex.quote(sys.executable)} -m app.cli $argv\nend\n"
            suffix = "fish"
        else:
            launcher = f'myclaude() {{ PYTHONPATH={shlex.quote(str(self.store.root / "backend"))} {shlex.quote(sys.executable)} -m app.cli "$@"; }}\n'
            suffix = shell
        folder = self.store.data / "terminal"
        folder.mkdir(exist_ok=True)
        path = folder / ("myclaude." + suffix)
        path.write_text(launcher + generate(parser(), shell, "myclaude") + "\n", encoding="utf-8")
        return {
            "shell": shell,
            "path": str(path),
            "script": path.read_text(encoding="utf-8"),
            "activate": f". '{path}'"
            if shell == "powershell"
            else "source " + shlex.quote(str(path)),
        }

    async def command(self, name, argument, session_id, permission, executor):
        def result(value):
            return {"kind": "result", "command": name, "result": value}

        parts = (
            shlex.split(argument)
            if name
            not in {"feedback", "speak", "stickers", "alias", "keybindings", "privacy-settings"}
            else []
        )
        if name in {"vim", "voice", "advisor", "notifications"}:
            if argument in {"", "on", "off"}:
                require(permission, "workspace-write")
                value = not self.prefs()[name] if not argument else argument == "on"
                prefs = self.preferences({name: value})
                return {
                    "kind": "ui",
                    "action": "feature-preferences",
                    "preferences": prefs,
                    "command": name,
                }
            if argument not in {"status", "list"}:
                raise ValueError(f"用法 /{name} [on|off|status]")
            return result(
                {
                    "preferences": self.prefs(),
                    "items": self.jobs.records("notification")[-100:]
                    if name == "notifications"
                    else [],
                }
            )
        if name == "keybindings":
            if not argument or argument == "show":
                return result(self.prefs()["keybindings"])
            require(permission, "workspace-write")
            keys = DEFAULT_KEYS if argument == "reset" else json.loads(argument)
            return {
                "kind": "ui",
                "action": "feature-preferences",
                "preferences": self.preferences({"keybindings": keys}),
            }
        if name == "privacy-settings":
            names = {
                "store_history": "privacy_store_history",
                "store_traces": "privacy_store_traces",
                "allow_cloud": "privacy_allow_cloud",
            }
            if argument:
                require(permission, "workspace-write")
                values = json.loads(argument)
                if (
                    not isinstance(values, dict)
                    or not set(values) <= set(names)
                    or any(type(v) is not bool for v in values.values())
                ):
                    raise ValueError("隐私设置仅支持 store_history/store_traces/allow_cloud 布尔值")
                if self.ext.jobs.agent and self.ext.jobs.agent.active:
                    raise ValueError("请等待运行结束后修改隐私设置")
                self.store.update({names[k]: v for k, v in values.items()})
            return result({k: getattr(self.store.current, v) for k, v in names.items()})
        if name == "rate-limit":
            if parts:
                require(permission, "workspace-write")
                if len(parts) not in {1, 2}:
                    raise ValueError("用法 /rate-limit 每分钟请求数 [并发数]")
                self.store.update(
                    {
                        "rate_limit_rpm": int(parts[0]),
                        "rate_limit_concurrency": int(parts[1])
                        if len(parts) == 2
                        else self.store.current.rate_limit_concurrency,
                    }
                )
            return result(
                {
                    "requests_per_minute": self.store.current.rate_limit_rpm,
                    "concurrency": self.store.current.rate_limit_concurrency,
                    "scope": "per-application-process",
                }
            )
        if name == "alias":
            args = shlex.split(argument)
            if not args or args[0] == "list":
                return result(self.aliases())
            require(permission, "workspace-write")
            if args[0] == "remove" and len(args) == 2:
                self.jobs.remove("command_alias", args[1])
                return result({"removed": args[1]})
            if (
                args[0] != "set"
                or len(args) < 3
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args[1])
            ):
                raise ValueError("用法 /alias set 名称 /命令 [参数]；/alias remove 名称")
            from .extensions import CATALOG

            reserved = {c["name"] for c in CATALOG["commands"]} | {
                a for c in CATALOG["commands"] for a in c["aliases"]
            }
            if args[1] in reserved:
                raise ValueError("别名不能覆盖内置命令")
            target = " ".join(args[2:])
            if not target.startswith("/"):
                raise ValueError("别名目标必须是 /命令")
            old = self.jobs.record("command_alias", args[1])
            self.jobs.put("command_alias", args[1], {"command": target})
            try:
                self.expand("/" + args[1])
            except ValueError:
                if old:
                    self.jobs.put("command_alias", args[1], {"command": old["command"]})
                else:
                    self.jobs.remove("command_alias", args[1])
                raise
            return result({"name": args[1], "command": target})
        if name == "macro":
            action = parts[0] if parts else "list"
            key = session_id or "local"
            if action == "list":
                return result(self.jobs.records("macro"))
            if action in {"record", "stop", "remove"}:
                require(permission, "workspace-write")
            if action == "stop":
                self.jobs.remove("macro_recording", key)
                return result({"recording": False})
            if len(parts) != 2 or not re.fullmatch(r"[\w-]{1,50}", parts[1]):
                raise ValueError("用法 /macro record|run|show|remove 名称；/macro stop")
            macro = parts[1]
            if action == "record":
                if self.jobs.record("macro", macro):
                    raise ValueError("宏已存在，请换名或显式删除")
                self.jobs.put("macro", macro, {"commands": [], "updated_at": now()})
                self.jobs.put("macro_recording", key, {"name": macro})
                return result({"recording": True, "name": macro})
            row = self.jobs.record("macro", macro)
            if not row:
                raise ValueError("宏不存在")
            if action == "show":
                return result(row)
            if action == "remove":
                self.jobs.remove("macro", macro)
                for recording in self.jobs.records("macro_recording"):
                    if recording["name"] == macro:
                        self.jobs.remove("macro_recording", recording["id"])
                return result({"removed": macro})
            if action != "run":
                raise ValueError("未知宏操作")
            if not row["commands"]:
                raise ValueError("宏中没有命令")
            values, active_session = [], session_id
            for text in row["commands"]:
                expanded = self.expand(text)
                if expanded.lstrip("/").split()[0] == "macro":
                    raise ValueError("宏不能嵌套运行宏")
                response = await executor(expanded, active_session, permission, _replay=True)
                if response["kind"] == "prompt":
                    require(permission, response.get("permission", permission))
                    if not self.ext.jobs.agent:
                        raise ValueError("Agent 执行器不可用")
                    active_session = (
                        active_session or self.ext.db.create_session("宏：" + macro)["id"]
                    )
                    final = None
                    async for event in self.ext.jobs.agent.run(
                        self.ext.db.session(active_session),
                        response["prompt"],
                        response.get("permission", permission),
                        self.store.current.model_copy(deep=True),
                    ):
                        if event["type"] == "run_end":
                            final = event
                    if not final or final["status"] != "completed":
                        raise ValueError("宏中的 Agent 步骤失败；后续步骤未执行")
                    response = {"kind": "run", "result": final}
                elif response["kind"] == "session":
                    active_session = response["session_id"]
                elif response["kind"] == "permission":
                    require(permission, response["permission"])
                    permission = response["permission"]
                values.append({"command": text, "response": response})
            return result({"name": macro, "steps": values, "session_id": active_session})
        if name == "share":
            if parts and parts[0] == "list":
                return result(self.shares())
            require(permission, "workspace-write")
            if parts and parts[0] == "revoke" and len(parts) == 2:
                return result(self.revoke_share(parts[1]))
            if parts and parts[0] != "create":
                raise ValueError("用法 /share [create [小时数]|list|revoke ID]")
            if len(parts) > 2:
                raise ValueError("用法 /share create [小时数]")
            return result(self.share(session_id, int(parts[1]) if len(parts) == 2 else 24))
        if name == "feedback":
            if not argument or argument == "list":
                return result(self.jobs.records("feedback"))
            require(permission, "workspace-write")
            return result(self.feedback(argument, session_id))
        if name == "stickers":
            if not argument or argument == "list":
                return {"kind": "ui", "action": "stickers", "packs": self.stickers()}
            require(permission, "workspace-write")
            if argument.startswith("remove "):
                key = argument[7:].strip()
                if key == "builtin":
                    raise ValueError("内置贴纸包不能删除")
                self.jobs.remove("sticker_pack", key)
                return result({"removed": key})
            return result(self.save_stickers(json.loads(argument)))
        if name in {"changelog", "release-notes"}:
            value = await release_notes(self.store.current.workspace, argument or None)
            return result(
                value
                if name == "changelog"
                else {"markdown": value["markdown"], "count": len(value["commits"])}
            )
        if name == "autofix":
            apply = bool(parts and parts[0] == "apply")
            target = (
                " ".join(parts[1:])
                if parts and parts[0] in {"apply", "preview"}
                else " ".join(parts)
            )
            return result(await autofix(self.ext, target or ".", apply, permission))
        if name == "migrate":
            if argument not in {"", "status", "plan", "apply"}:
                raise ValueError("用法 /migrate [status|plan|apply]")
            if argument == "apply":
                require(permission, "workspace-write")
                if self.ext.jobs.agent and self.ext.jobs.agent.active:
                    raise ValueError("请等待运行结束后执行数据迁移")
            return result(migrate(self.ext.db, self.store.data, argument == "apply"))
        if name == "benchmark":
            return result(await self.benchmark(int(argument or "1")))
        if name == "upgrade":
            if argument not in {"", "check", "apply"}:
                raise ValueError("用法 /upgrade [check|apply]")
            return result(await upgrade(self.store.root, argument == "apply", permission))
        if name == "terminal-setup":
            require(permission, "workspace-write")
            return result(self.terminal(argument or "powershell"))
        if name == "ide":
            if argument and argument not in {"vscode", "cursor"}:
                raise ValueError("支持 vscode/cursor")
            workspace = quote(str(Path(self.store.current.workspace)).replace("\\", "/"), safe="/")
            return {
                "kind": "ui",
                "action": "ide",
                "uri": (argument or "vscode") + "://file/" + workspace.lstrip("/"),
                "extension_path": "/api/features/ide/extension",
                "remote_note": "浏览器打开的是本机编辑器；服务器路径需使用 Remote SSH。",
            }
        if name == "desktop":
            return {
                "kind": "ui",
                "action": "desktop",
                "manifest": "manifest.webmanifest",
                "shortcut": "[InternetShortcut]\nURL={origin}\n",
            }
        if name == "image":
            if argument:
                from .media import Media

                path = safe_path(self.store.current.workspace, argument)
                return {
                    "kind": "attachment",
                    "items": [Media(self.ext).image(path.read_bytes(), path.name)],
                }
            return {"kind": "ui", "action": "image"}
        if name == "screenshot":
            return {"kind": "ui", "action": "screenshot"}
        if name == "listen":
            if argument:
                from .media import Media

                path = safe_path(self.store.current.workspace, argument)
                return {
                    "kind": "draft",
                    "text": await Media(self.ext).transcribe(path.read_bytes()),
                }
            return {"kind": "ui", "action": "listen"}
        if name == "speak":
            from .media import Media

            session = self.ext.db.session(session_id) if session_id else None
            text = argument or next(
                (
                    m.get("content")
                    for m in reversed((session or {}).get("messages", []))
                    if m["role"] == "assistant" and isinstance(m.get("content"), str)
                ),
                "",
            )
            if not text:
                raise ValueError("没有可朗读的回答；也可使用 /speak 文本")
            return {
                "kind": "ui",
                "action": "speak",
                "audio": await Media(self.ext).synthesize(text),
            }
        raise ValueError("未知功能命令")
