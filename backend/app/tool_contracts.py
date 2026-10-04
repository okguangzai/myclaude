"""Upstream tool payloads, retaining the Python runtime's containment and budgets."""

import json
import re
from copy import deepcopy

PREFERENCE_TYPES = {
    "theme": str,
    "editorMode": str,
    "verbose": bool,
    "preferredNotifChannel": str,
    "autoCompactEnabled": bool,
    "autoMemoryEnabled": bool,
    "autoDreamEnabled": bool,
    "fileCheckpointingEnabled": bool,
    "showTurnDuration": bool,
    "terminalProgressBarEnabled": bool,
    "todoFeatureEnabled": bool,
    "alwaysThinkingEnabled": bool,
    "language": str,
    "teammateMode": str,
}


def subagent_config(cfg, requested):
    raw = (requested or "general-purpose").strip()
    token = canonical_tool(raw)
    aliases = {
        "general": "general-purpose",
        "generalpurpose": "general-purpose",
        "generalpurposeagent": "general-purpose",
        "explore": "Explore",
        "explorer": "Explore",
        "exploreagent": "Explore",
        "plan": "Plan",
        "planagent": "Plan",
        "verification": "Verification",
        "verificationagent": "Verification",
        "verify": "Verification",
        "verifier": "Verification",
        "clawguide": "claw-guide",
        "clawguideagent": "claw-guide",
        "guide": "claw-guide",
        "statusline": "statusline-setup",
        "statuslinesetup": "statusline-setup",
    }
    kind = aliases.get(token, raw or "general-purpose")
    common = {
        "read_file",
        "glob_search",
        "grep_search",
        "WebFetch",
        "WebSearch",
        "ToolSearch",
        "StructuredOutput",
    }
    scopes = {
        "Explore": common | {"Skill"},
        "Plan": common | {"Skill", "TodoWrite", "SendUserMessage"},
        "Verification": common | {"bash", "PowerShell", "TodoWrite", "SendUserMessage"},
        "claw-guide": common | {"Skill", "SendUserMessage"},
        "statusline-setup": {
            "bash",
            "read_file",
            "write_file",
            "edit_file",
            "glob_search",
            "grep_search",
            "ToolSearch",
        },
    }
    from .policy import tool_allowed

    child = cfg.model_copy(deep=True)
    if kind in scopes:
        child.allowed_tools = sorted(name for name in scopes[kind] if tool_allowed(cfg, name))
        if not child.allowed_tools:
            child.allowed_tools = ["__no_tools_allowed__"]
    child.denied_tools = list(dict.fromkeys([*child.denied_tools, "AskUserQuestion"]))
    child.system_prompt += f"\nYou are a background sub-agent of type `{kind}`. Work only on the delegated task, use only available tools, do not ask the user questions, and finish with a concise result."
    return kind, child


def config_setting(store, cfg, args):
    name = args["setting"].strip()
    prefs = cfg.upstream_preferences
    fields = {"model", "temperature", "max_tokens", "max_turns", "context_chars"}
    if name not in PREFERENCE_TYPES and name not in fields and name != "permissions.defaultMode":
        raise PermissionError("Config 只可修改公开运行设置，不能访问连接凭据")
    previous = (
        getattr(cfg, name)
        if name in fields
        else cfg.permission_mode
        if name == "permissions.defaultMode"
        else prefs.get(name)
    )
    value = previous
    if "value" in args:
        value = args["value"]
        if name in PREFERENCE_TYPES:
            if PREFERENCE_TYPES[name] is bool:
                if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                    value = value.strip().lower() == "true"
                if not isinstance(value, bool):
                    raise ValueError("setting requires true or false")
            elif not isinstance(value, str):
                raise ValueError("setting requires a string value")
            options = {
                "editorMode": {"default", "vim", "emacs"},
                "teammateMode": {"tmux", "in-process", "auto"},
            }
            if name in options and value not in options[name]:
                raise ValueError("Invalid value for " + name)
            cfg.upstream_preferences = {**prefs, name: value}
            store.update({"upstream_preferences": cfg.upstream_preferences})
        elif name == "permissions.defaultMode":
            modes = {
                "default": "read-only",
                "plan": "read-only",
                "acceptEdits": "workspace-write",
                "dontAsk": "read-only",
                "auto": "read-only",
            }
            if value not in modes:
                raise ValueError("Invalid permissions.defaultMode")
            # Defaults never widen this run's permission; its grant lives in RunContext.
            store.update({"permission_mode": modes[value]})
        else:
            updated = type(cfg).model_validate({**cfg.model_dump(), name: value})
            setattr(cfg, name, getattr(updated, name))
            if name == "model":
                store.update({"model": cfg.model})
        value = getattr(cfg, name) if name in fields else value
    return {
        "success": True,
        "operation": "set" if "value" in args else "get",
        "setting": name,
        "value": value,
        "previousValue": previous if "value" in args else None,
        "newValue": value if "value" in args else None,
        "error": None,
        "scope": "current-run" if name in fields - {"model"} else "application",
    }


def canonical_tool(value):
    return re.sub(r"[^a-z0-9]", "", value.lower()).removesuffix("tool")


def expand_glob(pattern):
    patterns = [pattern]
    for _ in range(8):
        expanded, changed = [], False
        for item in patterns:
            match = re.search(r"\{([^{}]+)\}", item)
            if match and "," in match[1]:
                expanded.extend(
                    item[: match.start()] + part + item[match.end() :]
                    for part in match[1].split(",")
                )
                changed = True
            else:
                expanded.append(item)
            if len(expanded) > 64:
                raise ValueError("glob brace expansion exceeds 64 patterns")
        patterns = expanded
        if not changed:
            return patterns
    raise ValueError("glob brace expansion too deep")


def normalized_query(value):
    return " ".join(canonical_tool(part) for part in re.split(r"[\s,]+", value.strip()) if part)


def search_tools(query, limit, tools):
    query = query.strip()
    excluded = {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob_search",
        "grep_search",
        "list_files",
        "search_files",
        "retrieve_context",
    }
    specs = [tool for tool in tools if tool["function"]["name"] not in excluded]
    lowered = query.lower()
    if lowered.startswith("select:"):
        names = []
        for wanted in lowered[7:].split(","):
            item = next(
                (
                    tool["function"]["name"]
                    for tool in specs
                    if canonical_tool(tool["function"]["name"]) == canonical_tool(wanted)
                ),
                None,
            )
            if item:
                names.append(item)
    else:
        required = [term[1:] for term in lowered.split() if term.startswith("+") and len(term) > 1]
        optional = [term for term in lowered.split() if not term.startswith("+")]
        scored = []
        for tool in specs:
            spec = tool["function"]
            name, desc = spec["name"].lower(), spec.get("description", "")
            canonical = canonical_tool(name)
            haystack = f"{name} {desc.lower()} {canonical}"
            normalized = canonical + " " + normalized_query(desc)
            if any(term not in haystack for term in required):
                continue
            score = 0
            for term in required + optional:
                token = canonical_tool(term)
                score += 2 * (term in haystack) + 8 * (name == term) + 4 * (term in name)
                score += 12 * (canonical == token) + 3 * (token in normalized)
            if score or not lowered:
                scored.append((-score, spec["name"]))
        names = [name for _, name in sorted(scored)]
    names = names[: max(1, min(limit, 30))]
    by_name = {tool["function"]["name"]: tool for tool in specs}
    return {
        "matches": names,
        "query": query,
        "normalized_query": normalized_query(query),
        "total_deferred_tools": len(specs),
        "pending_mcp_servers": None,
        "tools": [by_name[name] for name in names],
    }


def lines(text):
    # Rust str::lines only recognizes LF/CRLF, not Python's other Unicode separators.
    return [line.removesuffix("\r") for line in text.removesuffix("\n").split("\n")] if text else []


def patch(original, updated):
    before, after = lines(original), lines(updated)
    return [
        {
            "oldStart": 1,
            "oldLines": len(before),
            "newStart": 1,
            "newLines": len(after),
            "lines": ["-" + line for line in before] + ["+" + line for line in after],
        }
    ]


def validate(name, args):
    fields = {
        "Agent": ("description", "prompt"),
        "TaskCreate": ("prompt",),
        "TaskUpdate": ("message",),
        "CronCreate": ("schedule", "prompt"),
        "TeamCreate": ("name",),
        "SendUserMessage": ("message",),
        "REPL": ("code", "language"),
        "AskUserQuestion": ("question",),
        "Skill": ("skill",),
        "Config": ("setting",),
    }
    for field in fields.get(name, ()):
        if not args[field].strip():
            raise ValueError(field + " must not be empty")
    if name == "TodoWrite":
        if not args["todos"]:
            raise ValueError("todos must not be empty")
        if any(
            not item[field].strip() for item in args["todos"] for field in ("content", "activeForm")
        ):
            raise ValueError("todo content and activeForm must not be empty")
    if name == "TeamCreate" and any(not task["prompt"].strip() for task in args["tasks"]):
        raise ValueError("task prompt must not be empty")
    if name == "StructuredOutput" and not args:
        raise ValueError("structured output payload must not be empty")


def notebook_edit(path, args):
    if path.suffix != ".ipynb":
        raise ValueError("File must be a Jupyter notebook (.ipynb file).")
    original = path.read_text(encoding="utf-8")
    notebook = json.loads(original)
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Notebook cells array not found")
    cells, mode = notebook["cells"], args.get("edit_mode", "replace")
    index = None
    if args.get("cell_id") is not None:
        index = next((i for i, cell in enumerate(cells) if cell.get("id") == args["cell_id"]), None)
        if index is None:
            raise ValueError("Cell id not found: " + args["cell_id"])
    elif mode != "insert":
        if not cells:
            raise ValueError("Notebook has no cells to edit")
        index = len(cells) - 1
    if mode != "delete" and "new_source" not in args:
        raise ValueError("new_source is required for insert and replace edits")
    source, cell_type = args.get("new_source", ""), None
    if mode == "delete":
        cell_id = cells.pop(index).get("id")
    else:
        cell_type = args.get(
            "cell_type", cells[index].get("cell_type", "code") if mode == "replace" else "code"
        )
        cell = deepcopy(cells[index]) if mode == "replace" else {"metadata": {}}
        if mode == "insert":
            number = len(cells) + 1
            while any(item.get("id") == f"cell-{number}" for item in cells):
                number += 1
            cell["id"] = f"cell-{number}"
        cell.update(cell_type=cell_type, source=source.splitlines(keepends=True) or [""])
        if cell_type == "code":
            # Changed code must never display execution results from its previous contents.
            cell.update(execution_count=None, outputs=[])
        else:
            cell.pop("execution_count", None)
            cell.pop("outputs", None)
        cell_id = cell.get("id")
        if mode == "insert":
            cells.insert(len(cells) if index is None else index + 1, cell)
        else:
            cells[index] = cell
    updated = json.dumps(notebook, ensure_ascii=False, indent=2)
    return {
        "new_source": source,
        "cell_id": cell_id,
        "cell_type": cell_type,
        "language": notebook.get("metadata", {}).get("kernelspec", {}).get("language", "python"),
        "edit_mode": mode,
        "error": None,
        "notebook_path": args["notebook_path"],
        "original_file": original,
        "updated_file": updated,
    }
