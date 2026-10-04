"""Named tool restrictions supplement (never widen) execution permission levels."""

import fnmatch
import json
import re


def tool_allowed(cfg, name):
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.denied_tools):
        return False
    return not cfg.allowed_tools or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in cfg.allowed_tools
    )


def rule_matches(rule, name, args):
    match = re.fullmatch(r"([^()]+)(?:\((.*)\))?", rule)
    if not match or not fnmatch.fnmatchcase(name.lower(), match[1].lower()):
        return False
    pattern = match[2]
    if pattern is None:
        return True
    value = args.get(
        "command", args.get("path", args.get("file_path", json.dumps(args, ensure_ascii=False)))
    )
    # Upstream command-prefix form Bash(git:*) is also accepted.
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return value == prefix or value.startswith(prefix + " ")
    return fnmatch.fnmatchcase(value, pattern)


async def authorize_rules(ext, cfg, name, args):
    rules = cfg.permission_rules
    if any(rule_matches(rule, name, args) for rule in rules.get("deny", [])):
        raise PermissionError("工具参数匹配 deny 规则：" + name)
    if any(rule_matches(rule, name, args) for rule in rules.get("allow", [])):
        return
    if any(rule_matches(rule, name, args) for rule in rules.get("ask", [])):
        answer = await ext.ask(
            "权限规则请求确认 "
            + name
            + "："
            + ext.store.redact(json.dumps(args, ensure_ascii=False)),
            ["允许本次", "拒绝"],
            cfg.timeout_seconds,
        )
        if answer != "允许本次":
            raise PermissionError("用户拒绝本次工具操作")
