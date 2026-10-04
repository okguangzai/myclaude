"""Rust v1 prompt-cache files and FNV-1a fingerprints (no Rust runtime needed)."""

import json
import os
import re
import tempfile
import time
from pathlib import Path


def stable_hash(raw):
    value = 0xCBF29CE484222325
    for byte in raw:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def sorted_value(value):
    if isinstance(value, dict):
        return {key: sorted_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sorted_value(item) for item in value]
    return value


def block(value):
    kind = value["type"]
    fields = {
        "text": ("text",),
        "thinking": ("thinking", "signature"),
        "tool_use": ("id", "name", "input"),
        "tool_result": ("tool_use_id", "content", "is_error"),
        "json": ("value",),
    }[kind]
    result = {"type": kind}
    for field in fields:
        item = value.get(field)
        if item is None or field == "is_error" and not item:
            continue
        result[field] = [block(part) for part in item] if field == "content" else sorted_value(item)
    return result


def request_value(request):
    """Serialize in Rust MessageRequest field order; JSON Value maps are sorted."""
    messages = []
    for message in request.get("messages", []):
        content = message.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        messages.append({"role": message["role"], "content": [block(v) for v in content]})
    result = {
        "model": request.get("model", ""),
        "max_tokens": request.get("max_tokens", 0),
        "messages": messages,
    }
    for field in (
        "system",
        "tools",
        "tool_choice",
        "stream",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "reasoning_effort",
        "extra_body",
    ):
        value = request.get(field)
        if value is None or field in {"stream", "extra_body"} and not value:
            continue
        if field == "tools":
            result[field] = []
            for tool in value:
                tool = tool.get("function", tool)
                entry = {"name": tool["name"]}
                if tool.get("description") is not None:
                    entry["description"] = tool["description"]
                entry["input_schema"] = sorted_value(
                    tool.get("input_schema", tool.get("parameters", {}))
                )
                result[field].append(entry)
        elif field == "tool_choice":
            result[field] = {
                "type": value["type"],
                **({"name": value["name"]} if "name" in value else {}),
            }
        else:
            result[field] = sorted_value(value)
    return result


def request_key(request):
    return f"v1-{stable_hash(json_bytes(request_value(request))):016x}"


def session_segment(session):
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", session)
    if len(sanitized) > 80:
        suffix = f"-{stable_hash(session.encode()):x}"
        sanitized = sanitized[: 80 - len(suffix)] + suffix
    return sanitized


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cache-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RustPromptCache:
    """Explicit file interoperability. Root is caller-controlled, never global by default."""

    def __init__(self, root, session, ttl=30):
        self.directory = Path(root) / session_segment(session)
        self.ttl = ttl

    def path(self, request):
        return self.directory / "completions" / (request_key(request) + ".json")

    def lookup(self, request, now=None):
        path = self.path(request)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        now = int(time.time()) if now is None else now
        if (
            entry.get("fingerprint_version", 1) != 1
            or max(0, now - entry["cached_at_unix_secs"]) >= self.ttl
        ):
            return None
        return entry["response"]

    def record(self, request, response, stats=None, now=None):
        now = int(time.time()) if now is None else now
        value = request_value(request)
        usage = response.get("usage", {})
        state = {
            "observed_at_unix_secs": now,
            "fingerprint_version": 1,
            **{
                key + "_hash": stable_hash(json_bytes(value.get(key)))
                for key in ("model", "system", "tools", "messages")
            },
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        }
        atomic_json(self.directory / "session-state.json", state)
        if stats is not None:
            atomic_json(self.directory / "stats.json", stats)
        if "id" in response and "content" in response and "model" in response:
            atomic_json(
                self.path(request),
                {"cached_at_unix_secs": now, "fingerprint_version": 1, "response": response},
            )
