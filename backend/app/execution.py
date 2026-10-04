"""Shared execution policy. A process working directory is NOT an OS sandbox."""

from __future__ import annotations

import asyncio
import contextvars
import os
import signal
import subprocess
from dataclasses import dataclass, field

LEVELS = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}


@dataclass
class RunContext:
    session_id: str = "local"
    run_id: str = ""
    permission: str = "read-only"
    depth: int = 0
    plan: bool = False
    budget: dict = field(default_factory=lambda: {"children": 0, "turns": 0})


current: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "run_context", default=None
)


def require(permission: str, needed: str):
    ctx = current.get()
    effective = "read-only" if ctx and ctx.plan else permission
    if LEVELS.get(effective, -1) < LEVELS[needed]:
        raise PermissionError(f"此操作需要 {needed} 权限；当前为 {effective}")


def clean_env(extra=None):
    # Explicit environment allow-list. LLM credentials are never inherited by tools.
    names = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "SYSTEMDRIVE",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    from .remote import inherited_env

    env.update(inherited_env(os.environ))
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    env.update(extra or {})
    return env


async def terminate(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def process(
    argv: list[str],
    cwd: str,
    *,
    stdin: str = "",
    timeout: float = 30,
    env: dict | None = None,
    limit: int = 128000,
):
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=clean_env(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )

    async def read(stream):
        data = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            remaining = max(0, limit - len(data))
            data.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        return bytes(data).decode("utf-8", errors="replace"), truncated

    async def write():
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    try:
        async with asyncio.timeout(min(max(timeout, 0.1), 900)):
            _, out, err, code = await asyncio.gather(
                write(), read(proc.stdout), read(proc.stderr), proc.wait()
            )
        return {
            "stdout": out[0],
            "stderr": err[0],
            "exit_code": code,
            "truncated": out[1] or err[1],
        }
    finally:
        await asyncio.shield(terminate(proc))
