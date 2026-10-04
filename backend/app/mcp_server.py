"""Expose this Python runtime as MCP over stdio (JSON lines or legacy Content-Length)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .agent import Agent
from .config import DATA, ROOT, ConfigStore
from .db import Database
from .extensions import Extensions
from .oauth import OAuth
from .providers import Provider
from .rag import RagService
from .tools import Tools
from .tunnel import Tunnel

MAX_FRAME = 2_000_000


def read_frame(stream):
    first = stream.readline(MAX_FRAME + 1)
    if not first:
        return None, False
    if len(first) > MAX_FRAME:
        raise ValueError("MCP frame exceeds limit")
    if first.lower().startswith(b"content-length:"):
        length = int(first.split(b":", 1)[1].strip())
        if not 0 < length <= MAX_FRAME:
            raise ValueError("invalid Content-Length")
        headers = 0
        while True:
            header = stream.readline(8192)
            headers += len(header)
            if header in {b"\r\n", b"\n"}:
                break
            if not header or headers > 16384:
                raise ValueError("invalid MCP headers")
        payload = stream.read(length)
        if len(payload) != length:
            raise ValueError("truncated MCP frame")
        return payload, True
    return first, False


def write_frame(stream, value, framed):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw if framed else raw + b"\n")
    stream.flush()


class McpServer:
    def __init__(self, extensions, permission="read-only"):
        self.ext, self.permission = extensions, permission

    async def dispatch(self, request):
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
        ):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "invalid request"},
            }
        if "id" not in request:
            return None
        response = {"jsonrpc": "2.0", "id": request["id"]}
        method, params = request["method"], request.get("params") or {}
        cfg = self.ext.store.current
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "myclaude", "version": "0.6.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": s["function"]["name"],
                        "description": s["function"].get("description", ""),
                        "inputSchema": s["function"]["parameters"],
                    }
                    for s in self.ext.tools.definitions(self.permission, cfg.rag_enabled)
                ]
            }
        elif method == "tools/call":
            try:
                output = await self.ext.execute(
                    cfg.model_copy(deep=True),
                    self.permission,
                    params["name"],
                    params.get("arguments", {}),
                )
                result = {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "isError": False,
                }
            except Exception as error:
                result = {
                    "content": [{"type": "text", "text": self.ext.store.redact(str(error))}],
                    "isError": True,
                }
        else:
            return {**response, "error": {"code": -32601, "message": "method not found: " + method}}
        return {**response, "result": result}

    async def serve(self, incoming, outgoing):
        while True:
            raw, framed = await asyncio.to_thread(read_frame, incoming)
            if raw is None:
                return
            try:
                request = json.loads(raw)
            except ValueError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                response = await self.dispatch(request)
            if response is not None:
                write_frame(outgoing, response, framed)


async def run(args):
    store = ConfigStore(args.data_dir, ROOT)
    if args.workspace:
        store.current = type(store.current).model_validate(
            {**store.current.model_dump(), "workspace": args.workspace}
        )
    db = Database(args.data_dir / "app.sqlite", recover=False)
    async with httpx.AsyncClient(trust_env=False) as client:
        ext, tunnel = Extensions(db, store, client), Tunnel()
        provider = Provider(client, OAuth(store, client), store)
        tools = Tools(RagService(db, provider, client, tunnel), ext)
        ext.jobs.agent = Agent(db, provider, tools, store)
        try:
            await McpServer(ext, args.permission).serve(sys.stdin.buffer, sys.stdout.buffer)
        finally:
            await ext.close()
            await tunnel.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument(
        "--permission",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="read-only",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
