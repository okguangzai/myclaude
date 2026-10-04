"""Five-layer upstream settings discovery with provenance and explicit migration.

Reading layers does not execute project hooks, start MCP commands or install plugins.
Startup applies layers in memory; explicit import persists supported fields.
"""

import json
import os
from copy import deepcopy
from pathlib import Path

SECRET_NAMES = {
    "apikey",
    "api_key",
    "token",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "authorization",
    "password",
}


def mask(value, key=""):
    normalized = key.lower().replace("-", "")
    if (
        normalized in SECRET_NAMES
        or normalized in {"env", "headers", "runtime_environment"}
        or normalized.endswith(("_key", "_token", "secret", "password"))
    ):
        return "[REDACTED]" if value else ""
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def discover(cwd, config_home=None):
    home = Path(config_home or os.environ.get("CLAW_CONFIG_HOME", str(Path.home() / ".claw")))
    cwd = Path(cwd)
    return [
        ("user", home.parent / ".claw.json"),
        ("user", home / "settings.json"),
        ("project", cwd / ".claw.json"),
        ("project", cwd / ".claw/settings.json"),
        ("local", cwd / ".claw/settings.local.json"),
    ]


def load(cwd, config_home=None):
    merged, provenance, files = {}, {}, []

    def merge(target, incoming, path, prefix=""):
        for key, value in incoming.items():
            full = prefix + key
            if isinstance(value, dict):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                merge(target[key], value, path, full + ".")
            else:
                target[key] = deepcopy(value)
                provenance[full] = path

    for rank, (scope, path) in enumerate(discover(cwd, config_home), 1):
        info = {"source": scope, "path": str(path), "precedence_rank": rank, "status": "not_found"}
        if path.exists():
            try:
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    raise ValueError("配置必须是小于 1 MB 的文件")
                parsed = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(parsed, dict):
                    raise ValueError("配置根节点必须为对象")
                merge(merged, parsed, str(path))
                info["status"] = "loaded"
            except (ValueError, OSError) as error:
                info.update(status="load_error", error=type(error).__name__)
        files.append(info)
    return {"merged": merged, "files": files, "provenance": provenance}


def translate(source):
    if not isinstance(source, dict):
        raise ValueError("配置根节点必须为对象")
    for section in (
        "provider",
        "permissions",
        "hooks",
        "plugins",
        "providerFallbacks",
        "apiTimeout",
        "oauth",
    ):
        if section in source and not isinstance(source[section], dict):
            raise ValueError(f"配置 {section} 必须为对象")
    changes, consumed = {}, set()
    mapping = {
        "model": "model",
        "subagentModel": "subagent_model",
        "trustedRoots": "trusted_roots",
        "aliases": "model_aliases",
        "rulesImport": "rules_import",
        "sandbox": "sandbox_config",
        "enabledPlugins": "plugin_enabled",
        "env": "runtime_environment",
        "mcpServers": "upstream_mcp_servers",
    }
    for old, new in mapping.items():
        if old in source:
            changes[new] = source[old]
            consumed.add(old)
    from .tool_contracts import PREFERENCE_TYPES

    preferences = {key: source[key] for key in PREFERENCE_TYPES if key in source}
    if preferences:
        changes["upstream_preferences"] = preferences
        consumed.update(preferences)
    if "$schema" in source:
        consumed.add("$schema")
    provider = source.get("provider", {})
    for old, new in {
        "kind": "provider",
        "apiKey": "api_key",
        "baseUrl": "base_url",
        "model": "model",
    }.items():
        if old in provider:
            value = provider[old]
            changes[new] = (
                {"openai-compatible": "openai", "openai_compatible": "openai"}.get(value, value)
                if old == "kind"
                else value
            )
            consumed.add("provider." + old)
    permissions = source.get("permissions", {})
    if "defaultMode" in permissions:
        mode = permissions["defaultMode"]
        changes["permission_mode"] = {
            "plan": "read-only",
            "default": "read-only",
            "acceptEdits": "workspace-write",
            "bypassPermissions": "danger-full-access",
            "dontAsk": "read-only",
        }.get(mode, mode)
        consumed.add("permissions.defaultMode")
    if "permissionMode" in source:
        mode = source["permissionMode"]
        changes["permission_mode"] = {
            "plan": "read-only",
            "default": "read-only",
            "dontAsk": "read-only",
            "acceptEdits": "workspace-write",
            "bypassPermissions": "danger-full-access",
        }.get(mode, mode)
        consumed.add("permissionMode")
    if "deniedTools" in permissions:
        changes["denied_tools"] = permissions["deniedTools"]
        consumed.add("permissions.deniedTools")
    rules = {key: permissions[key] for key in ("allow", "deny", "ask") if key in permissions}
    if rules:
        changes["permission_rules"] = rules
        consumed.update("permissions." + key for key in rules)
    hooks = {}
    for event, entries in source.get("hooks", {}).items():
        if event not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            continue
        hooks[event] = []
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} 必须为数组")
        for entry in entries:
            if isinstance(entry, str):
                hooks[event].append(entry)
            elif not isinstance(entry, dict):
                raise ValueError(f"hooks.{event} 项必须为字符串或对象")
            elif "hooks" in entry:
                if not isinstance(entry["hooks"], list) or not all(
                    isinstance(hook, dict) and isinstance(hook.get("command"), str)
                    for hook in entry["hooks"]
                ):
                    raise ValueError(f"hooks.{event}.hooks 必须为含 command 的对象数组")
                hooks[event].extend(
                    {
                        "command": hook["command"],
                        "matcher": entry.get("matcher", "*"),
                        "timeout": hook.get("timeout", 20),
                    }
                    for hook in entry["hooks"]
                    if hook.get("type", "command") == "command"
                )
            else:
                hooks[event].append(entry)
        consumed.add("hooks." + event)
    if hooks:
        changes["hooks"] = hooks
    for old, new in {
        "externalDirectories": "plugin_external_directories",
        "bundledRoot": "plugin_bundled_root",
        "enabled": "plugin_enabled",
        "installRoot": "plugin_install_root",
        "registryPath": "plugin_registry_path",
        "maxOutputTokens": "plugin_max_output_tokens",
    }.items():
        if old in source.get("plugins", {}):
            changes[new] = source["plugins"][old]
            consumed.add("plugins." + old)
    for key, target in {"primary": "fallback_primary", "fallbacks": "fallback_models"}.items():
        if key in source.get("providerFallbacks", {}):
            changes[target] = source["providerFallbacks"][key]
            consumed.add("providerFallbacks." + key)
    for key, target in {
        "requestTimeout": "timeout_seconds",
        "connectTimeout": "connect_timeout_seconds",
        "maxRetries": "provider_retries",
    }.items():
        if key in source.get("apiTimeout", {}):
            changes[target] = source["apiTimeout"][key]
            consumed.add("apiTimeout." + key)
    if "oauth" in source:
        from .oauth import OAuthConfig

        raw = source["oauth"]
        changes["oauth_config"] = OAuthConfig(
            client_id=raw["clientId"],
            authorize_url=raw["authorizeUrl"],
            token_url=raw["tokenUrl"],
            scopes=raw.get("scopes", []),
            redirect_uri=raw.get("manualRedirectUrl")
            or f"http://127.0.0.1:{raw.get('callbackPort', 8000)}/api/oauth/callback",
        ).model_dump()
        consumed.add("oauth")

    def leaves(value, prefix=""):
        for key, item in value.items():
            full = prefix + key
            if full in consumed or not isinstance(item, dict):
                yield full
            else:
                yield from leaves(item, full + ".")

    return {"changes": changes, "unmapped": sorted(set(leaves(source)) - consumed)}


def activate(store):
    """Apply workspace layers to this process only, retaining unconsumed fields for diagnostics."""
    report = load(store.current.workspace)
    translated = translate(report["merged"])
    store.current = type(store.current).model_validate(
        {**store.current.model_dump(), **translated["changes"]}
    )
    store.upstream_config = report["merged"]
    store.layer_report = mask({**report, **translated})
    store.remember_runtime_secrets()
    return store.layer_report


def inspect(store, config_home=None, apply=False):
    report = load(store.current.workspace, config_home)
    translated = translate(report["merged"])
    if apply:
        if any(f["status"] == "load_error" for f in report["files"]):
            raise ValueError("配置存在读取错误，未导入")
        if translated["unmapped"]:
            raise ValueError("存在未映射配置，未导入：" + ", ".join(translated["unmapped"]))
        store.update(translated["changes"])
    return mask({**report, **translated, "applied": apply})
