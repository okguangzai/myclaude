from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import Settings

DENIED = {
    ".git",
    ".env",
    ".local",
    ".venv",
    "node_modules",
    ".ssh",
    ".aws",
    ".claw-local",
    "__pycache__",
}


def safe_path(workspace: str, value: str) -> Path:
    root = Path(workspace).resolve()
    raw = Path(value)
    if raw.is_absolute() or raw.drive or ".." in raw.parts:
        raise ValueError("路径必须相对工作区，且不能包含 ..")
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError("路径或符号链接超出工作区")
    for part in (*raw.parts, *path.relative_to(root).parts):
        if (
            part.lower() in DENIED
            or part.lower().startswith(".env")
            or part.lower().endswith((".pem", ".key", ".pfx"))
        ):
            raise ValueError("凭据或内部状态目录禁止由文件工具访问")
    return path


def definition(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def definitions(permission: str, rag: bool) -> list:
    string = {"type": "string"}
    specs = [
        definition(
            "read_file",
            "读取工作区 UTF-8 文本文件，最多 60000 字符。路径相对工作区。",
            {"path": string},
            ["path"],
        ),
        definition(
            "list_files",
            "列出工作区子目录中的文件和文件夹，不递归，最多 200 项。",
            {"path": string},
            [],
        ),
        definition(
            "search_files",
            "在工作区文本文件内搜索字面字符串，最多返回 40 个命中。",
            {"query": string},
            ["query"],
        ),
    ]
    if permission == "workspace-write":
        specs.append(
            definition(
                "write_file",
                "写入或覆盖工作区 UTF-8 文本文件。仅在用户任务需要时使用。",
                {"path": string, "content": string},
                ["path", "content"],
            )
        )
    if rag:
        specs.append(
            definition(
                "retrieve_context",
                "对已上传知识库做语义检索，返回来源、片段、分数。回答时引用来源。",
                {
                    "query": string,
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                ["query"],
            )
        )
    return specs


class Tools:
    def __init__(self, rag, extensions=None):
        self.rag = rag
        self.extensions = extensions
        if extensions:
            extensions.tools = self

    def definitions(self, permission, rag):
        if not self.extensions:
            return definitions(permission, rag)
        specs = self.extensions.definitions(permission)
        names = {s["function"]["name"] for s in specs}
        combined = specs + [
            s
            for s in definitions(permission, rag)
            if s["function"]["name"] not in names
            and s["function"]["name"] in {"list_files", "search_files", "retrieve_context"}
        ]
        from .policy import tool_allowed

        return [
            s
            for s in combined
            if tool_allowed(self.extensions.store.current, s["function"]["name"])
        ]

    async def execute(self, cfg: Settings, permission: str, name: str, args: dict):
        if self.extensions:
            return await self.extensions.execute(cfg, permission, name, args)
        return await self.execute_builtin(cfg, permission, name, args)

    async def execute_builtin(self, cfg: Settings, permission: str, name: str, args: dict):
        allowed = {d["function"]["name"] for d in definitions(permission, cfg.rag_enabled)}
        if name not in allowed:
            raise ValueError("工具未获当前权限授权")
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        if name == "retrieve_context":
            return await self.rag.query(cfg, str(args["query"])[:4000], args.get("top_k", 5))
        if name == "read_file":
            path = safe_path(cfg.workspace, args["path"])
            if path.stat().st_size > 256000:
                raise ValueError("文件过大，最多读取 256 KB")
            text = path.read_text(encoding="utf-8")
            return {
                "path": args["path"],
                "content": text[:60000],
                "truncated": len(text) > 60000,
            }
        if name == "list_files":
            path = safe_path(cfg.workspace, args.get("path", "."))
            entries = []
            for p in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                try:
                    safe_path(cfg.workspace, str(p.relative_to(Path(cfg.workspace))))
                except ValueError:
                    continue
                entries.append({"name": p.name, "type": "directory" if p.is_dir() else "file"})
                if len(entries) >= 200:
                    break
            return {"entries": entries}
        if name == "search_files":
            query = args["query"]
            if not isinstance(query, str) or not query or len(query) > 500:
                raise ValueError("搜索字符串长度必须为 1–500")
            matches, visited = [], 0
            root = Path(cfg.workspace)
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d.lower() not in DENIED and not d.startswith(".")]
                for name in files:
                    visited += 1
                    if visited > 500:
                        return {"matches": matches, "truncated": True}
                    relative = str((Path(directory) / name).relative_to(root))
                    try:
                        path = safe_path(cfg.workspace, relative)
                        if path.stat().st_size > 128000:
                            continue
                        lines = path.read_text(encoding="utf-8").splitlines()
                    except (ValueError, OSError, UnicodeError):
                        continue
                    for n, line in enumerate(lines, 1):
                        if query.casefold() in line.casefold():
                            matches.append({"path": relative, "line": n, "text": line[:400]})
                            if len(matches) >= 40:
                                return {"matches": matches, "truncated": True}
            return {"matches": matches, "truncated": False}
        if name == "write_file":
            path = safe_path(cfg.workspace, args["path"])
            content = args["content"]
            if not isinstance(content, str) or len(content.encode("utf-8")) > 256000:
                raise ValueError("写入内容必须为文本且不超过 256 KB")
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_path(cfg.workspace, args["path"])
            handle, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            return {
                "path": args["path"],
                "bytes": len(content.encode("utf-8")),
                "written": True,
                "filePath": str(path),
            }
        raise ValueError("未知工具")
