"""Installed skill and agent definitions with local/workspace scope precedence."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import yaml


def invocation_name(value):
    value = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lstrip("/$").lower()).strip("-")
    if not value or len(value) > 80:
        raise ValueError("名称必须包含字母或数字，且不超过 80 字符")
    return value


def frontmatter(text):
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    header, separator, _ = text[4:].partition("\n---")
    if not separator:
        return {}
    if len(header) > 65536:
        raise ValueError("定义头部超过 64 KB")
    for index, event in enumerate(yaml.parse(header)):
        if index > 2048 or isinstance(event, yaml.AliasEvent):
            raise ValueError("定义头部过于复杂或包含 YAML alias")
    metadata = yaml.safe_load(header) or {}
    if not isinstance(metadata, dict):
        raise ValueError("定义 frontmatter 必须为对象")
    return json.loads(json.dumps(metadata, default=str))


class Assets:
    def __init__(self, store, plugins=None):
        self.store, self.plugins = store, plugins

    def roots(self, kind):
        workspace = Path(self.store.current.workspace)
        return [
            *(self.plugins.asset_roots(kind) if self.plugins else []),
            self.store.data / kind,
            workspace / kind,
            workspace / ".claude" / kind,
            workspace / ".claw" / kind,
        ]

    def list(self, kind):
        if kind not in {"skills", "agents"}:
            raise ValueError("未知定义类型")
        records = {}
        for root in self.roots(kind):
            if not root.exists():
                continue
            for file in sorted(root.iterdir()):
                path = file / "SKILL.md" if file.is_dir() else file
                if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                if path.suffix not in {".md", ".toml"}:
                    continue
                if path.stat().st_size > 65536:
                    continue
                text = path.read_text(encoding="utf-8-sig")
                meta = tomllib.loads(text) if path.suffix == ".toml" else frontmatter(text)
                name = file.name if file.is_dir() else file.stem
                records[name] = {
                    "name": name,
                    "path": str(path),
                    "scope": "user" if root == self.store.data / kind else "project",
                    "metadata": meta,
                }
        return sorted(records.values(), key=lambda x: x["name"])

    def read(self, kind, name):
        name = invocation_name(name)
        item = next((x for x in self.list(kind) if x["name"].lower() == name), None)
        if not item:
            raise ValueError(f"未找到 {kind}: {name}")
        path = Path(item["path"])
        if path.stat().st_size > 65536:
            raise ValueError("定义文件超过 64 KB")
        return {**item, "content": path.read_text(encoding="utf-8-sig")}

    def install_skill(self, source):
        source = Path(source).expanduser()
        if not source.is_absolute():
            source = Path(self.store.current.workspace) / source
        source = source.resolve(strict=True)
        prompt = source / "SKILL.md" if source.is_dir() else source
        if not prompt.is_file() or prompt.suffix.lower() != ".md":
            raise ValueError("技能源应为 Markdown 文件或含 SKILL.md 的目录")
        text = prompt.read_text(encoding="utf-8-sig")
        if len(text.encode("utf-8")) > 65536:
            raise ValueError("技能定义超过 64 KB")
        name = invocation_name(
            frontmatter(text).get("name") or (source.name if source.is_dir() else source.stem)
        )
        target = self.store.data / "skills" / name
        if target.exists():
            raise ValueError("技能已安装，请先卸载")
        # Validate the entire tree before copying; never follow a junction outside its source.
        entries = list(source.rglob("*")) if source.is_dir() else [source]
        if (
            len(entries) > 1000
            or sum(p.stat().st_size for p in entries if p.is_file()) > 10_000_000
        ):
            raise ValueError("技能目录超过大小限制")
        root = source if source.is_dir() else source.parent
        if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in entries):
            raise ValueError("技能不能包含符号链接或越界目录联接")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir()
            shutil.copy2(source, target / "SKILL.md")
        return self.read("skills", name)

    def uninstall_skill(self, name):
        root = (self.store.data / "skills").resolve()
        target = (root / invocation_name(name)).resolve()
        if target.parent != root:
            raise ValueError("技能路径越界")
        if target.exists():
            shutil.rmtree(target)
        return {"removed": name, "skills": self.list("skills")}

    def create_agent(
        self, name, description="Describe when to use this agent.", prompt="", model=None
    ):
        name = invocation_name(name)
        root = Path(self.store.current.workspace) / ".claw" / "agents"
        root.mkdir(parents=True, exist_ok=True)
        path = root / (name + ".toml")
        values = {
            "name": name,
            "description": description,
            "model_reasoning_effort": "medium",
            "prompt": prompt,
        }
        if model:
            values["model"] = model
        with path.open("x", encoding="utf-8") as file:
            file.write(
                "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items())
                + "\n"
            )
        return self.read("agents", name)
