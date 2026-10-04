"""Real Python language-server requests over stdio JSON-RPC; no regex pseudo-LSP."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .execution import clean_env, terminate

LANGUAGES = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".vue": "vue",
}


async def query(workspace, path, action, line=0, character=0, servers=None):
    language = LANGUAGES.get(path.suffix)
    argv = (servers or {}).get(language)
    if not argv and language == "python":
        argv = [sys.executable, "-m", "pylsp"]
    if not argv or not isinstance(argv, list) or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("请在设置 lsp_servers 中配置该语言的服务器命令")
    argv = [sys.executable if p == "$python" else p for p in argv]
    action = {
        "goto_definition": "definition",
        "document_symbols": "symbols",
        "find_references": "references",
        "formatting": "format",
        "completions": "completion",
    }.get(action, action)
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=clean_env(),
        **options,
    )
    serial, notifications = 0, []

    async def send(method, params, wait=True):
        nonlocal serial
        serial += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if wait:
            message["id"] = serial
        raw = json.dumps(message).encode()
        proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        await proc.stdin.drain()
        if not wait:
            return
        while True:
            item = await receive()
            if item.get("id") == serial:
                if "error" in item:
                    raise ValueError(str(item["error"]))
                return item.get("result")
            notifications.append(item)

    async def receive():
        size = 0
        while True:
            header = await proc.stdout.readline()
            if not header:
                raise ValueError("语言服务器提前退出")
            if header == b"\r\n":
                break
            if header.lower().startswith(b"content-length:"):
                size = int(header.split(b":")[1])
        if not 0 < size < 2_000_000:
            raise ValueError("语言服务器响应长度异常")
        return json.loads(await proc.stdout.readexactly(size))

    try:
        async with asyncio.timeout(25):
            await send(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": Path(workspace).as_uri(),
                    "capabilities": {},
                    "initializationOptions": {
                        "pylsp": {"plugins": {"jedi": {"environment": sys.prefix}}}
                    },
                },
            )
            await send("initialized", {}, False)
            uri = path.as_uri()
            await send(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language,
                        "version": 1,
                        "text": path.read_text(encoding="utf-8"),
                    }
                },
                False,
            )
            if action == "diagnostics":
                while True:
                    msg = await receive()
                    if (
                        msg.get("method") == "textDocument/publishDiagnostics"
                        and msg["params"].get("uri") == uri
                    ):
                        return msg["params"]
            method = {
                "symbols": "documentSymbol",
                "references": "references",
                "definition": "definition",
                "hover": "hover",
                "completion": "completion",
                "format": "formatting",
            }[action]
            params = {"textDocument": {"uri": uri}}
            if action not in {"symbols", "format"}:
                params["position"] = {"line": line, "character": character}
            if action == "format":
                params["options"] = {"tabSize": 4, "insertSpaces": True}
            if action == "references":
                params["context"] = {"includeDeclaration": True}
            return await send("textDocument/" + method, params)
    finally:
        await asyncio.shield(terminate(proc))
