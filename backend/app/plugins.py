from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .execution import current, process, require


class Plugins:
    def __init__(self, jobs, store, *, staging=False):
        self.jobs, self.store = jobs, store

        def configured_path(value, default):
            path = Path(value).expanduser() if value else default
            return (
                (Path(store.current.workspace) / path).resolve()
                if not path.is_absolute()
                else path.resolve()
            )

        self.home = configured_path(store.current.plugin_install_root, store.data / "plugins")
        self.registry_path = (
            configured_path(store.current.plugin_registry_path, store.data / "plugins.json")
            if store.current.plugin_registry_path and not staging
            else None
        )
        self.home.mkdir(parents=True, exist_ok=True)
        self.started = set()
        self.mutation_lock = asyncio.Lock()
        self.importing = True
        if self.registry_path and self.registry_path.is_file():
            self.import_registry()
        self.importing = False

    def import_registry(self):
        """Read upstream installed.json without enabling imported executable code."""
        root = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
        for record in root.get("plugins", {}).values():
            name = record["name"]
            if self.jobs.record("plugin", name):
                continue
            path = Path(record["install_path"]).expanduser().resolve()
            target = self.home / name
            if path != target.resolve():
                item = self.install(path)
            else:
                manifest = json.loads(
                    (path / ".claude-plugin/plugin.json").read_text(encoding="utf-8-sig")
                )
                if (
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,29}", name)
                    or manifest["name"] != name
                ):
                    raise ValueError("插件注册表名称与 manifest 不匹配")
                item = {
                    "name": name,
                    "manifest": manifest,
                    "digest": self.digest(path),
                    "enabled": False,
                }
            source = record.get("source", {})
            item.pop("id", None)
            item.update(
                source=source.get("url") or source.get("path") or str(path),
                kind=record.get("kind", "external"),
                installed_at_unix_ms=record.get("installed_at_unix_ms", int(time.time() * 1000)),
            )
            self.jobs.put("plugin", name, item)

    def persist_registry(self):
        if not self.registry_path or self.importing:
            return
        records = {}
        for item in self.list():
            name, manifest = item["name"], item["manifest"]
            source = item.get("source", str(self.home / name))
            remote = urlsplit(source).scheme in {"https", "ssh"} or source.startswith("git@")
            records[name] = {
                "id": name,
                "kind": item.get("kind", "external"),
                "name": name,
                "version": manifest.get("version", "0.0.0"),
                "description": manifest.get("description", ""),
                "install_path": str(self.home / name),
                "source": {"type": "git_url", "url": source}
                if remote
                else {"type": "local_path", "path": source},
                "installed_at_unix_ms": item.get("installed_at_unix_ms", int(time.time() * 1000)),
                "updated_at_unix_ms": int(time.time() * 1000),
            }
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"plugins": records}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.registry_path)

    def put(self, kind, key, value):
        result = self.jobs.put(kind, key, value)
        self.persist_registry()
        return result

    def list(self):
        return self.jobs.records("plugin")

    def discover(self):
        cfg = self.store.current
        roots = [("bundled", cfg.plugin_bundled_root or str(Path(__file__).with_name("bundled")))]
        roots.extend(("external", path) for path in cfg.plugin_external_directories)
        candidates, failures, seen = [], [], set()
        for kind, value in roots:
            root = Path(value).expanduser()
            if not root.is_absolute():
                root = Path(cfg.workspace) / root
            root = root.resolve()
            if not root.exists():
                continue
            try:
                paths = (
                    [root]
                    if (root / ".claude-plugin/plugin.json").is_file()
                    else sorted(p for p in root.iterdir() if p.is_dir())
                )
                for path in paths:
                    manifest_path = path / ".claude-plugin/plugin.json"
                    if not manifest_path.is_file():
                        continue
                    try:
                        self.digest(path)
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                        name = manifest["name"]
                        if name in seen:
                            raise ValueError("插件名称重复：" + name)
                        seen.add(name)
                        candidates.append(
                            {"name": name, "kind": kind, "source": str(path), "manifest": manifest}
                        )
                    except (OSError, ValueError, KeyError) as error:
                        failures.append(
                            {
                                "source": str(path),
                                "kind": kind,
                                "error": self.store.redact(str(error)),
                            }
                        )
            except OSError as error:
                failures.append({"source": str(root), "kind": kind, "error": type(error).__name__})
        return {"candidates": candidates, "failures": failures}

    def registry(self):
        report = self.discover()
        installed = []
        for item in self.list():
            try:
                intact = self.digest(self.home / item["name"]) == item["digest"]
            except (OSError, ValueError):
                intact = False
            installed.append({**item, "healthy": intact, "active": item["name"] in self.started})
        return {
            **report,
            "installed": installed,
            "builtin": [
                {
                    "name": "example-builtin",
                    "kind": "builtin",
                    "version": "0.1.0",
                    "enabled": False,
                    "tools": [],
                    "status": "upstream-scaffold",
                }
            ],
        }

    async def sync(self):
        report = self.discover()
        async with self.mutation_lock:
            for candidate in report["candidates"]:
                name = candidate["name"]
                try:
                    old = self.jobs.record("plugin", name)
                    if old and old.get("source") != candidate["source"]:
                        raise ValueError("同名已安装插件来源不同，保留已安装版本")
                    if not old:
                        item = self.install(candidate["source"])
                    elif old["digest"] != self.digest(Path(candidate["source"])):
                        await self._update(name, candidate["source"])
                        item = self.jobs.record("plugin", name)
                    else:
                        item = old
                    item.pop("id", None)
                    item["kind"] = candidate["kind"]
                    self.put("plugin", name, item)
                    enabled = self.store.current.plugin_enabled
                    requested = enabled.get(
                        name + "@" + candidate["kind"], enabled.get(name, item["enabled"])
                    )
                    if requested != item["enabled"]:
                        await self._enable(name, requested)
                except (OSError, ValueError, KeyError) as error:
                    report["failures"].append(
                        {
                            "source": candidate["source"],
                            "kind": candidate["kind"],
                            "error": self.store.redact(str(error)),
                        }
                    )
            active_sources = {
                item["source"] for item in report["candidates"] if item["kind"] == "bundled"
            }
            for item in self.list():
                if (
                    item.get("kind") == "bundled"
                    and item["source"] not in active_sources
                    and not report["failures"]
                ):
                    await self._uninstall(item["name"])
        for failure in report["failures"]:
            self.jobs.put(
                "plugin_error", hashlib.sha256(failure["source"].encode()).hexdigest()[:12], failure
            )
        return report

    def asset_roots(self, kind):
        roots = []
        for item in self.list():
            if item["enabled"]:
                _, root = self.verified(item["name"])
                if (root / kind).is_dir():
                    roots.append(root / kind)
        return roots

    def digest(self, root):
        digest = hashlib.sha256()
        files = sorted(root.rglob("*"))
        if len(files) > 500:
            raise ValueError("插件文件数量超过 500")
        total = 0
        for path in files:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("插件不能包含符号链接或目录联接")
            if path.is_file():
                total += path.stat().st_size
                if total > 10_000_000:
                    raise ValueError("插件超过 10 MB")
                digest.update(path.relative_to(root).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def install(self, source):
        source = Path(source).resolve()
        self.digest(source)
        manifest = json.loads(
            (source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8-sig")
        )
        name = manifest["name"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,29}", name):
            raise ValueError("插件名必须是 1–30 位字母、数字、下划线或连字符")
        for tool in manifest.get("tools", []):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,29}", tool["name"]):
                raise ValueError("插件工具名称不合法")
            if len(f"plugin__{name}__{tool['name']}") > 64:
                raise ValueError("插件名和工具名组合不能超过模型工具名的 64 字符限制")
            Draft202012Validator.check_schema(tool["inputSchema"])
        if self.jobs.record("plugin", name):
            raise ValueError("插件已安装；请先卸载再安装新版本")
        target = self.home / name
        if target.exists():
            raise ValueError("目标插件目录已存在")
        shutil.copytree(source, target)
        return self.put(
            "plugin",
            name,
            {
                "name": name,
                "enabled": False,
                "manifest": manifest,
                "digest": self.digest(target),
                "source": str(source),
            },
        )

    async def install_source(self, source):
        if urlsplit(source).scheme in {"https", "ssh"} or source.startswith("git@"):
            with tempfile.TemporaryDirectory(prefix="plugin-", dir=self.store.data) as temporary:
                checkout = Path(temporary) / "checkout"
                result = await process(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=",
                        "clone",
                        "--depth",
                        "1",
                        "--",
                        source,
                        str(checkout),
                    ],
                    str(self.store.data),
                    timeout=120,
                )
                if result["exit_code"]:
                    raise ValueError(
                        "插件 Git 源克隆失败：" + self.store.redact(result["stderr"])[:500]
                    )
                metadata = checkout / ".git"
                if metadata.is_dir() and metadata.resolve().parent == checkout.resolve():

                    def clear_read_only(operation, filename, error):
                        path = Path(filename)
                        if (
                            not path.resolve().is_relative_to(metadata.resolve())
                            or path.is_symlink()
                        ):
                            raise ValueError("Git 元数据删除路径越界")
                        if not isinstance(error[1], PermissionError):
                            raise error[1]
                        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                        operation(path)

                    shutil.rmtree(metadata, onerror=clear_read_only)
                item = self.install(str(checkout))
                key = item.pop("id")
                item["source"] = source
                return self.put("plugin", key, item)
        return self.install(source)

    async def update(self, name, source=None):
        async with self.mutation_lock:
            return await self._update(name, source)

    async def _update(self, name, source=None):
        old = self.jobs.record("plugin", name)
        if not old:
            raise ValueError("插件未安装")
        source = source or old.get("source")
        if not source:
            raise ValueError("旧安装记录没有来源，请提供更新源路径")
        # Stage with an isolated registry. Failed validation never removes the working version.
        with tempfile.TemporaryDirectory(prefix="plugin-update-", dir=self.store.data) as directory:

            class Staging:
                def __init__(self):
                    self.rows = {}

                def record(self, kind, key):
                    return self.rows.get(key)

                def put(self, kind, key, value):
                    self.rows[key] = {"id": key, **value}
                    return self.rows[key]

            staged = Plugins(Staging(), self.store, staging=True)
            staged.home = Path(directory) / "staged"
            staged.home.mkdir()
            new = await staged.install_source(source)
            if new["id"] != name:
                raise ValueError("更新源的插件名称与已安装插件不一致")
            target = (self.home / name).resolve()
            if target.parent != self.home.resolve():
                raise ValueError("插件路径越界")
            was_enabled = old["enabled"]
            if was_enabled:
                await self._enable(name, False)
            backup = Path(directory) / "previous"
            target.rename(backup)
            try:
                (staged.home / name).rename(target)
                self.put("plugin", name, {k: v for k, v in new.items() if k != "id"})
                if was_enabled:
                    await self._enable(name, True)
            except BaseException:
                if target.exists() and target.parent == self.home.resolve():
                    shutil.rmtree(target)
                backup.rename(target)
                self.put("plugin", name, {k: v for k, v in old.items() if k != "id"})
                if was_enabled:
                    await self._enable(name, True)
                raise
            return {
                "plugin": self.jobs.record("plugin", name),
                "old_version": old["manifest"].get("version"),
                "new_version": new["manifest"].get("version"),
            }

    def verified(self, name):
        item = self.jobs.record("plugin", name)
        if not item or not item["enabled"]:
            raise ValueError("插件不存在或未启用")
        root = self.home / name
        if self.digest(root) != item["digest"]:
            raise ValueError("插件文件在安装后被修改，请重新安装并审查")
        return item, root

    def argv(self, root, command, args=None):
        parts = shlex.split(command, posix=True)
        if not parts:
            raise ValueError("插件命令为空")
        parts += args or []
        if parts[0] in {"python", "$python", "python3"}:
            parts[0] = sys.executable
        elif parts[0].startswith(("./", ".\\")):
            path = (root / parts[0]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("插件命令路径越界或不存在")
            parts[0] = str(path)
        return parts

    async def invoke(self, name, command, payload, args=None):
        _, root = self.verified(name)
        result = await process(
            self.argv(root, command, args),
            str(root),
            stdin=json.dumps(payload, ensure_ascii=False),
            timeout=20,
            env={"MYCLAUDE_WORKSPACE": self.store.current.workspace},
        )
        if result["exit_code"] != 0:
            raise ValueError(
                f"插件 {name} 退出码 {result['exit_code']}：{self.store.redact(result['stderr'])[:400]}"
            )
        try:
            return json.loads(result["stdout"] or "{}")
        except ValueError:
            return {"text": result["stdout"]}

    async def enable(self, name, enabled):
        async with self.mutation_lock:
            return await self._enable(name, enabled)

    async def _enable(self, name, enabled):
        item = self.jobs.record("plugin", name)
        if not item:
            raise ValueError("插件不存在")
        if enabled and item["enabled"] and name in self.started:
            self.verified(name)
            return item
        if not enabled and name in self.started:
            await self.lifecycle(name, "Shutdown")
            self.started.discard(name)
        item.pop("id")
        item["enabled"] = enabled
        self.put("plugin", name, item)
        if enabled:
            try:
                self.verified(name)
                await self.lifecycle(name, "Init")
            except BaseException:
                item["enabled"] = False
                self.put("plugin", name, item)
                raise
            self.started.add(name)
        return self.jobs.record("plugin", name)

    async def lifecycle(self, name, event):
        item, _ = self.verified(name)
        for command in item["manifest"].get("lifecycle", {}).get(event, []):
            await self.invoke(name, command, {"event": event})

    async def startup(self):
        await self.sync()
        for item in self.list():
            if item["enabled"]:
                try:
                    await self.enable(item["id"], True)
                except Exception as exc:
                    self.jobs.put(
                        "plugin_error", item["id"], {"error": self.store.redact(str(exc))}
                    )

    async def close(self):
        for name in list(self.started):
            try:
                await self.lifecycle(name, "Shutdown")
            except Exception:
                pass

    def definitions(self, permission):
        if permission != "danger-full-access":
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": f"plugin__{item['id']}__{t['name']}",
                    "description": t.get("description", ""),
                    "parameters": t["inputSchema"],
                },
            }
            for item in self.list()
            if item["enabled"]
            for t in item["manifest"].get("tools", [])
        ]

    async def execute(self, permission, name, args):
        require(permission, "danger-full-access")
        _, plugin, tool = name.split("__", 2)
        item, _ = self.verified(plugin)
        spec = next((t for t in item["manifest"].get("tools", []) if t["name"] == tool), None)
        if not spec:
            raise ValueError("插件工具不存在")
        Draft202012Validator(spec["inputSchema"]).validate(args)
        return await self.invoke(plugin, spec["command"], args, spec.get("args", []))

    def aliases(self):
        from collections import Counter

        pairs = [
            (tool["name"], f"plugin__{item['id']}__{tool['name']}")
            for item in self.list()
            if item["enabled"]
            for tool in item["manifest"].get("tools", [])
        ]
        counts = Counter(name for name, _ in pairs)
        return {
            name: full
            for name, full in pairs
            if counts[name] == 1 and not name.startswith(("mcp__", "plugin__"))
        }

    async def hooks(self, event, name, args, permission, output=None):
        # Hooks execute arbitrary local code and require the explicit execution permission.
        if permission != "danger-full-access" or (current.get() and current.get().plan):
            return args
        for plugin in self.list():
            if not plugin["enabled"]:
                continue
            for command in plugin["manifest"].get("hooks", {}).get(event, []):
                result = await self.invoke(
                    plugin["id"],
                    command,
                    {
                        "hook_event_name": event,
                        "tool_name": name,
                        "tool_input": args,
                        "tool_response": output,
                    },
                )
                if not isinstance(result, dict):
                    raise ValueError("Hook 必须输出 JSON 对象")
                ctx = current.get()
                if ctx and ctx.run_id:
                    self.jobs.db.event(
                        ctx.run_id,
                        "hook",
                        json.loads(
                            self.store.redact(
                                json.dumps(
                                    {"event": event, "plugin": plugin["id"], "result": result}
                                )
                            )
                        ),
                    )
                specific = result.get("hookSpecificOutput", result)
                if event == "PreToolUse":
                    if (
                        specific.get("permissionDecision") == "deny"
                        or result.get("decision") == "block"
                    ):
                        raise PermissionError(
                            specific.get(
                                "permissionDecisionReason",
                                result.get("reason", "插件 hook 阻止此操作"),
                            )
                        )
                    args = specific.get("updatedInput", args)
        return args

    async def uninstall(self, name):
        async with self.mutation_lock:
            return await self._uninstall(name)

    async def _uninstall(self, name):
        await self._enable(name, False)
        target = (self.home / name).resolve()
        if target.parent != self.home.resolve():
            raise ValueError("插件路径越界")
        shutil.rmtree(target)
        self.jobs.remove("plugin", name)
        self.persist_registry()
        return {"removed": name}
