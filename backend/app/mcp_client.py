"""MCP clients own their SDK context in a single task (AnyIO cancel-scope affinity)."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import clean_env, require


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,29}$")
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list, max_length=30)
    url: str = ""
    env_refs: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    permission: Literal["read-only", "workspace-write", "danger-full-access"] = "danger-full-access"
    enabled: bool = False
    timeout: int = Field(default=30, ge=1, le=120)
    startup_retries: int = Field(default=0, ge=0, le=3)

    @field_validator("url")
    @classmethod
    def url_check(cls, value):
        from urllib.parse import urlsplit

        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("MCP 地址只接受无凭据、无查询参数的 HTTP(S) URL")
        return value


class Connection:
    def __init__(self, config: ServerConfig, cwd, env, log_dir, lifecycle=None):
        self.config, self.cwd, self.env = config, cwd, env
        self.log_dir = log_dir
        self.lifecycle = lifecycle
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.serve())

    async def serve(self):
        waiting = None
        try:
            async with AsyncExitStack() as stack:
                cfg = self.config
                if cfg.transport == "stdio":
                    params = StdioServerParameters(
                        command=sys.executable if cfg.command == "$python" else cfg.command,
                        args=cfg.args,
                        cwd=self.cwd,
                        env=clean_env(self.env),
                    )
                    log_path = Path(self.log_dir) / "mcp-stderr.log"
                    # SDK server stderr can contain secrets: keep private; never put it in API replies.
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log = stack.enter_context(log_path.open("a", encoding="utf-8"))
                    streams = await stack.enter_async_context(stdio_client(params, errlog=log))
                else:
                    headers = {k: self.env[v] for k, v in cfg.header_refs.items()}
                    transport = (
                        streamablehttp_client(cfg.url, headers=headers, timeout=cfg.timeout)
                        if cfg.transport == "streamable-http"
                        else sse_client(cfg.url, headers=headers, timeout=cfg.timeout)
                    )
                    streams = await stack.enter_async_context(transport)
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                if self.lifecycle:
                    self.lifecycle.record("initialize_handshake")
                async with asyncio.timeout(cfg.timeout):
                    await session.initialize()
                if self.lifecycle:
                    self.lifecycle.record("ready")
                self.ready.set_result(True)
                while True:
                    method, args, waiting = await self.queue.get()
                    try:
                        async with asyncio.timeout(cfg.timeout):
                            result = await getattr(session, method)(**args)
                        if not waiting.done():
                            waiting.set_result(result.model_dump(mode="json", by_alias=True))
                    except Exception as exc:
                        if not waiting.done():
                            waiting.set_exception(
                                RuntimeError(f"MCP {method} 失败：{type(exc).__name__}")
                            )
                    finally:
                        waiting = None
        except BaseException as exc:
            error = RuntimeError(f"MCP 连接关闭或初始化失败：{type(exc).__name__}")
            if self.lifecycle and not isinstance(exc, asyncio.CancelledError):
                self.lifecycle.record("error_surfacing", error=error, recoverable=True)
            if not self.ready.done():
                self.ready.set_exception(error)
            if waiting is not None and not waiting.done():
                waiting.set_exception(error)
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(error)

    async def call(self, method, **args):
        await asyncio.wait_for(asyncio.shield(self.ready), self.config.timeout + 5)
        if self.task.done():
            raise RuntimeError("MCP 连接已关闭，请重连")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((method, args, future))
        try:
            return await asyncio.wait_for(future, self.config.timeout + 5)
        except asyncio.CancelledError:
            # Closing the connection cancels outstanding calls and owned stdio process.
            await self.close()
            raise

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class McpManager:
    def __init__(self, jobs, store):
        self.jobs, self.store = jobs, store
        self.connections = {}
        self.locks = {}
        self.cache = {}
        self.configured = {}
        self.configuration_errors = []
        self.project_secrets = {}
        raw_servers = getattr(store, "upstream_config", {}).get("mcpServers", {})
        for name, raw in raw_servers.items():
            try:
                transport = raw.get("type", "stdio" if raw.get("command") else "sse")
                transport = {"http": "streamable-http", "streamableHttp": "streamable-http"}.get(
                    transport, transport
                )
                refs = {}
                for section in ("env", "headers"):
                    refs[section] = {}
                    for index, (key, value) in enumerate(raw.get(section, {}).items()):
                        reference = f"MYCLAUDE_MCP_{name}_{section}_{index}"
                        refs[section][key] = reference
                        self.project_secrets[reference] = str(value)
                cfg = ServerConfig(
                    name=name,
                    transport=transport,
                    command=raw.get("command", ""),
                    args=raw.get("args", []),
                    url=raw.get("url", ""),
                    env_refs=refs["env"],
                    header_refs=refs["headers"],
                    enabled=not raw.get("disabled", False),
                )
                self.configured[name] = cfg.model_dump()
            except (ValueError, TypeError, AttributeError) as error:
                self.configuration_errors.append(
                    {"server_name": name, "status": "invalid_config", "error": type(error).__name__}
                )
        store.extra_secrets.update(v for v in self.project_secrets.values() if len(v) >= 8)

    def list(self):
        merged = {item["name"]: item for item in self.jobs.records("mcp")}
        merged.update({name: {"id": name, **cfg} for name, cfg in self.configured.items()})
        return list(merged.values())

    def health(self):
        states = {item["server_name"]: item for item in self.jobs.records("mcp_health")}
        result = []
        for config in self.list():
            name = config["name"]
            state = dict(
                states.get(name, {"server_name": name, "status": "starting", "events": []})
            )
            conn = self.connections.get(name)
            state["connected"] = bool(conn and not conn.task.done())
            if not state["connected"] and state["status"] in {"healthy", "degraded", "starting"}:
                state["status"] = "stopped"
            result.append(state)
        return [*result, *self.configuration_errors]

    async def configure(self, value):
        from .lifecycle import Lifecycle

        cfg = ServerConfig.model_validate(value)
        if cfg.transport == "stdio" and not cfg.command:
            raise ValueError("stdio 必须配置 command")
        if cfg.transport != "stdio" and not cfg.url:
            raise ValueError("HTTP/SSE 必须配置 url")
        await self.disconnect(cfg.name)
        self.configured.pop(cfg.name, None)
        Lifecycle(self.jobs, cfg.name).record("server_registration")
        return self.jobs.put("mcp", cfg.name, cfg.model_dump())

    async def disconnect(self, name):
        conn = self.connections.pop(name, None)
        self.cache.pop(name, None)
        if conn:
            from .lifecycle import Lifecycle

            lifecycle = Lifecycle(self.jobs, name)
            lifecycle.record("shutdown")
            await conn.close()
            lifecycle.record("cleanup")

    async def connect(self, name, permission):
        raw = next((dict(item) for item in self.list() if item["name"] == name), None)
        if not raw:
            raise ValueError("MCP 服务不存在")
        raw.pop("id", None)
        cfg = ServerConfig.model_validate(raw)
        require(permission, cfg.permission)
        if not cfg.enabled:
            raise ValueError("MCP 服务尚未启用")
        async with self.locks.setdefault(name, asyncio.Lock()):
            conn = self.connections.get(name)
            if conn and not conn.task.done():
                return conn
            from dotenv import dotenv_values

            secrets = {
                **dotenv_values(self.store.root / ".env"),
                **getattr(self.store, "runtime_environment", {}),
                **os.environ,
                **self.project_secrets,
            }
            refs = set(cfg.env_refs.values()) | set(cfg.header_refs.values())
            missing = [r for r in refs if not secrets.get(r)]
            if missing:
                raise ValueError("缺少 MCP 环境变量：" + ", ".join(missing))
            env = {k: str(secrets[v]) for k, v in cfg.env_refs.items()}
            env.update({r: str(secrets[r]) for r in cfg.header_refs.values()})
            self.store.extra_secrets.update(value for value in env.values() if len(value) >= 8)
            from .lifecycle import Lifecycle

            lifecycle = Lifecycle(self.jobs, name)
            for attempt in range(cfg.startup_retries + 1):
                lifecycle.record("spawn_connect", attempt=attempt)
                conn = Connection(
                    cfg, self.store.current.workspace, env, self.store.data, lifecycle
                )
                self.connections[name] = conn
                try:
                    await asyncio.wait_for(asyncio.shield(conn.ready), cfg.timeout + 5)
                    return conn
                except asyncio.CancelledError:
                    await self.disconnect(name)
                    raise
                except Exception as error:
                    await self.disconnect(name)
                    lifecycle.record(
                        "error_surfacing",
                        error=type(error).__name__,
                        recoverable=attempt < cfg.startup_retries,
                        attempt=attempt,
                    )
                    if attempt == cfg.startup_retries:
                        raise
                    await asyncio.sleep(min(2**attempt, 4))

    async def call(self, server_name, permission, method, **args):
        from .lifecycle import Lifecycle

        lifecycle = Lifecycle(self.jobs, server_name)
        conn = await self.connect(server_name, permission)
        phase = {"list_tools": "tool_discovery", "list_resources": "resource_discovery"}.get(
            method, "invocation"
        )
        try:
            result = await conn.call(method, **args)
            lifecycle.record(phase, method=method)
        except Exception as error:
            lifecycle.record(phase, error=type(error).__name__, recoverable=True, method=method)
            raise
        collection = {
            "list_tools": "tools",
            "list_resources": "resources",
            "list_prompts": "prompts",
            "list_resource_templates": "resourceTemplates",
        }.get(method)
        if collection:
            seen = set()
            while result.get("nextCursor"):
                cursor = result["nextCursor"]
                if cursor in seen or len(seen) >= 100:
                    raise ValueError("MCP 分页游标重复或超过 100 页")
                seen.add(cursor)
                page = await conn.call(method, **{**args, "cursor": cursor})
                result.setdefault(collection, []).extend(page.get(collection, []))
                result["nextCursor"] = page.get("nextCursor")
        import json

        result = json.loads(self.store.redact(json.dumps(result, ensure_ascii=False)))
        if method == "list_tools":
            self.cache[server_name] = result.get("tools", [])
        return result

    async def discover(self, name, permission):
        result = {}
        for method in ("list_tools", "list_resources", "list_prompts"):
            try:
                result[method] = await self.call(name, permission, method)
            except Exception as exc:
                result[method] = {"error": str(exc)}
        from .lifecycle import Lifecycle

        errors = [value["error"] for value in result.values() if "error" in value]
        Lifecycle(self.jobs, name).record(
            "ready", error="; ".join(errors) if len(errors) == 3 else None, degraded=bool(errors)
        )
        return result

    async def close(self):
        await asyncio.gather(
            *(self.disconnect(name) for name in list(self.connections)), return_exceptions=True
        )
        self.connections.clear()
