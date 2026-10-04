"""Configured tool hooks with matcher, exit-code semantics and validated JSON rewrites."""

import json
import os
import re
import shutil

from .execution import current, process


async def run_hooks(ext, event, name, args, permission, output=None):
    if permission != "danger-full-access" or (current.get() and current.get().plan):
        return args
    cfg = ext.store.current
    for entry in cfg.hooks.get(event, []):
        spec = {"command": entry} if isinstance(entry, str) else entry
        matcher = spec.get("matcher", "*")
        if matcher != "*" and name not in matcher.split("|"):
            continue
        command = spec.get("command")
        if not command:
            raise ValueError("Hook command 不能为空")
        if isinstance(command, list):
            argv = ext.plugins.argv(ext.store.root, command[0], command[1:])
        elif os.name == "nt":
            argv = [
                shutil.which("pwsh") or "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
        else:
            argv = ["sh", "-c", command]
        result = await process(
            argv,
            cfg.workspace,
            stdin=json.dumps(
                {
                    "hook_event_name": event,
                    "tool_name": name,
                    "tool_input": args,
                    "tool_response": output,
                }
            ),
            timeout=min(spec.get("timeout", 20), 120),
        )
        try:
            parsed = json.loads(result["stdout"] or "{}")
        except ValueError:
            parsed = {"systemMessage": result["stdout"]}
        if not isinstance(parsed, dict):
            parsed = {"systemMessage": "Hook 输出不是 JSON 对象"}
        context = current.get()
        if context and context.run_id:
            ext.db.event(
                context.run_id,
                "hook",
                json.loads(
                    ext.store.redact(
                        json.dumps(
                            {
                                "event": event,
                                "source": "settings",
                                "exit_code": result["exit_code"],
                                "result": parsed,
                            }
                        )
                    )
                ),
            )
        specific = parsed.get("hookSpecificOutput", {})
        if (
            result["exit_code"] == 2
            or parsed.get("continue") is False
            or parsed.get("decision") == "block"
            or specific.get("permissionDecision") == "deny"
        ):
            raise PermissionError(
                ext.store.redact(
                    specific.get("permissionDecisionReason")
                    or parsed.get("reason")
                    or result["stdout"]
                    or "Hook 阻止操作"
                )
            )
        if result["exit_code"]:
            raise ValueError(f"Hook 失败，退出码 {result['exit_code']}")
        if event == "PreToolUse":
            if specific.get("permissionDecision") == "ask":
                answer = await ext.ask(
                    "Hook 请求确认工具 "
                    + name
                    + "："
                    + ext.store.redact(json.dumps(args, ensure_ascii=False)),
                    ["允许本次", "拒绝"],
                    cfg.timeout_seconds,
                )
                if answer != "允许本次":
                    raise PermissionError("用户拒绝本次工具操作")
            args = specific.get("updatedInput", args)
    return args


def validate_hooks(value):
    if any(event not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} for event in value):
        raise ValueError("未知 Hook 事件")
    for entries in value.values():
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError("每个事件最多配置 20 个 Hook")
        for entry in entries:
            spec = {"command": entry} if isinstance(entry, str) else entry
            if not isinstance(spec, dict) or not spec.get("command"):
                raise ValueError("Hook 必须有 command")
            command = spec["command"]
            if not isinstance(command, str) and not (
                isinstance(command, list) and all(isinstance(x, str) and x for x in command)
            ):
                raise ValueError("Hook command 必须是字符串或非空 argv 字符串列表")
            timeout = spec.get("timeout", 20)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ValueError("Hook timeout 必须在 0–120 秒之间")
            if not re.fullmatch(r"[A-Za-z0-9_*|_-]+", spec.get("matcher", "*")):
                raise ValueError("Hook matcher 使用工具名、| 或 *")
    return value
