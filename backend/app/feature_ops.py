"""Executable maintenance operations: migrations, reversible fixes and local upgrades."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .execution import process, require
from .git_context import git
from .tools import safe_path

MIGRATIONS = [
    (
        1,
        "index_session_runs",
        "CREATE INDEX IF NOT EXISTS runs_session_status ON runs(session_id,status)",
    ),
    (
        2,
        "index_document_namespace",
        "CREATE INDEX IF NOT EXISTS documents_namespace ON documents(namespace)",
    ),
]


def migration_status(db):
    with db.connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT,applied_at TEXT)"
        )
        rows = {r["version"]: dict(r) for r in con.execute("SELECT * FROM schema_migrations")}
    result = []
    for version, name, sql in MIGRATIONS:
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if version in rows and rows[version]["checksum"] != checksum:
            raise ValueError("已应用的数据迁移校验和不一致，拒绝继续")
        result.append({"version": version, "name": name, "applied": version in rows})
    unknown = set(rows) - {v for v, _, _ in MIGRATIONS}
    if unknown:
        raise ValueError("数据库来自更新版本，当前程序不能降级迁移")
    return {"migrations": result, "pending": sum(not r["applied"] for r in result)}


def migrate(db, data, apply=False):
    before = migration_status(db)
    if not apply or not before["pending"]:
        return {**before, "applied_now": [], "backup": None}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = Path(data) / "backups" / ("migration-" + stamp + ".sqlite")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db.path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    applied = []
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        for version, name, sql in MIGRATIONS:
            if con.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone():
                continue
            con.execute(sql)
            con.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                (version, name, hashlib.sha256(sql.encode()).hexdigest(), stamp),
            )
            applied.append(version)
    return {**migration_status(db), "applied_now": applied, "backup": str(backup)}


async def autofix(ext, relative=".", apply=False, permission="read-only"):
    if apply:
        require(permission, "workspace-write")
    cfg = ext.store.current
    selected = safe_path(cfg.workspace, relative)
    candidates = [selected] if selected.is_file() else list(ext.paths(cfg, relative))
    files = [p for p in candidates if p.suffix == ".py"]
    if not files:
        raise ValueError("当前自动修复器为 Ruff；指定包含 Python 文件的工作区路径")
    if len(files) > 100:
        raise ValueError("一次最多修复 100 个 Python 文件，请缩小路径")
    originals, fixed, diagnostics = {}, {}, []
    with tempfile.TemporaryDirectory(prefix="myclaude-fix-") as folder:
        temp = Path(folder)
        for path in files:
            path = safe_path(cfg.workspace, path.relative_to(cfg.workspace).as_posix())
            if path.stat().st_size > 256000:
                raise ValueError("自动修复单个文件不能超过 256 KB")
            key = path.relative_to(cfg.workspace).as_posix()
            originals[key] = path.read_text(encoding="utf-8")
            copy = temp / key
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(originals[key], encoding="utf-8")
        # --isolated avoids executing or inheriting unreviewed workspace configuration.
        run = await process(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--fix",
                "--output-format",
                "json",
                str(temp),
            ],
            str(temp),
            timeout=30,
        )
        if run["exit_code"] not in {0, 1}:
            raise ValueError("Ruff 执行失败：" + run["stderr"][:500])
        for item in json.loads(run["stdout"] or "[]"):
            diagnostics.append(
                {
                    "path": Path(item["filename"]).relative_to(temp).as_posix(),
                    "code": item["code"],
                    "message": item["message"],
                    "location": item["location"],
                }
            )
        for key, original in originals.items():
            content = (temp / key).read_text(encoding="utf-8")
            if content != original:
                fixed[key] = content
    changes = [
        {
            "path": key,
            "diff": "".join(
                difflib.unified_diff(
                    originals[key].splitlines(True),
                    value.splitlines(True),
                    fromfile=key,
                    tofile=key,
                )
            ),
        }
        for key, value in fixed.items()
    ]
    backup = None
    if apply and fixed:
        for key, original in originals.items():
            if safe_path(cfg.workspace, key).read_text(encoding="utf-8") != original:
                raise ValueError("文件在预览期间发生变化，请重试")
        backup = (
            ext.store.data
            / "backups"
            / ("autofix-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        )
        backup.mkdir(parents=True)
        (backup / "originals.json").write_text(
            json.dumps(originals, ensure_ascii=False), encoding="utf-8"
        )
        for key, content in fixed.items():
            await ext.tools.execute_builtin(
                cfg, permission, "write_file", {"path": key, "content": content}
            )
    return {
        "mode": "apply" if apply else "preview",
        "engine": "ruff-safe-fixes",
        "checked": len(files),
        "changes": changes,
        "remaining_diagnostics": diagnostics,
        "backup": str(backup) if backup else None,
    }


async def release_notes(workspace, reference=None):
    args = ["log", "-30", "--format=%h%x09%s%x09%ad", "--date=short"]
    if reference:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,150}", reference) or reference.startswith("-"):
            raise ValueError("Git 基线格式无效")
        revision = await git(
            workspace, "rev-parse", "--verify", "--end-of-options", reference + "^{commit}"
        )
        args.append(revision + "..HEAD")
    try:
        raw = await git(workspace, *args)
    except ValueError:
        if reference:
            raise
        manifest = json.loads(
            Path(__file__).with_name("release_manifest.json").read_text(encoding="utf-8")
        )
        return {**manifest, "source": "application-release", "commits": []}
    commits = [
        {"hash": p[0], "subject": p[1], "date": p[2]}
        for line in raw.splitlines()
        if len(p := line.split("\t", 2)) == 3
    ]
    return {
        "commits": commits,
        "markdown": "# 发布说明\n\n"
        + (
            "\n".join(f"- {c['subject']} (`{c['hash']}` · {c['date']})" for c in commits)
            or "没有新增提交。"
        ),
    }


def deployment_pipeline_url():
    value = os.environ.get("MYCLAUDE_PIPELINE_URL", "").strip()
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme == "https" and url.hostname and not url.username and not url.password:
        return value
    return None


async def upgrade(root, apply=False, permission="read-only"):
    root = Path(root)
    if not (root / ".git").exists():
        return {
            "mode": "managed-ci",
            "version": "0.8.0",
            "message": "容器实例由部署方更新；网页可重新加载当前版本。",
            "pipeline_url": deployment_pipeline_url(),
            "client_action": "reload",
        }
    if apply:
        require(permission, "danger-full-access")
    head = await git(root, "rev-parse", "HEAD")
    branch = await git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise ValueError("游离 HEAD 不能自动升级")
    # Checking remote availability never changes the checkout.
    remote = await git(root, "ls-remote", "--exit-code", "origin", "refs/heads/" + branch)
    latest = remote.split()[0]
    if not apply or head == latest:
        return {
            "mode": "git",
            "current": head,
            "latest": latest,
            "update_available": head != latest,
            "applied": False,
        }
    if await git(root, "status", "--porcelain"):
        raise ValueError("存在未提交文件，不能自动升级；请先提交或备份")
    result = await process(["git", "pull", "--ff-only", "origin", branch], str(root), timeout=120)
    if result["exit_code"]:
        raise ValueError("升级失败，未强制重置本地代码：" + result["stderr"][:400])
    requirements = root / "backend/requirements.lock.txt"
    install = await process(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)], str(root), timeout=120
    )
    if install["exit_code"]:
        raise ValueError("代码已更新，但依赖安装失败；请运行 install.ps1 完成安装")
    import os

    npm = "npm.cmd" if os.name == "nt" else "npm"
    for args in ([npm, "ci"], [npm, "run", "build"]):
        build = await process(args, str(root / "frontend"), timeout=300)
        if build["exit_code"]:
            raise ValueError(
                "代码已更新，但前端构建失败；请在 frontend 执行 npm ci 和 npm run build 后再重启"
            )
    return {
        "mode": "git",
        "previous": head,
        "current": await git(root, "rev-parse", "HEAD"),
        "applied": True,
        "restart_required": True,
    }
