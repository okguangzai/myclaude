"""Read-only Git startup context, base checks and stale-branch policy decisions."""

from pathlib import Path

from .execution import process


async def git(cwd, *args):
    result = await process(["git", "--no-optional-locks", *args], cwd, timeout=10)
    if result["exit_code"]:
        raise ValueError("Git 查询失败：" + result["stderr"][:500])
    return result["stdout"].strip()


async def detect(cwd):
    try:
        if await git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
            return None
        branch = await git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        log = await git(cwd, "log", "-5", "--format=%h %s")
        staged = await git(cwd, "diff", "--cached", "--name-only")
        return {
            "branch": None if branch == "HEAD" else branch,
            "recent_commits": [
                {"hash": line.partition(" ")[0], "subject": line.partition(" ")[2]}
                for line in log.splitlines()
            ],
            "staged_files": staged.splitlines(),
        }
    except (ValueError, OSError):
        return None


async def check_base(cwd, flag=None):
    source, expected = "flag", (flag or "").strip()
    if not expected:
        path = Path(cwd) / ".claw-base"
        source = "file"
        if path.is_file() and path.stat().st_size < 4096:
            expected = path.read_text(encoding="utf-8").strip()
    if not expected:
        return {"state": "no_expected_base"}
    try:
        head = await git(cwd, "rev-parse", "--verify", "HEAD^{commit}")
    except (ValueError, OSError):
        return {"state": "not_a_git_repo", "source": source}
    try:
        resolved = await git(
            cwd, "rev-parse", "--verify", "--end-of-options", expected + "^{commit}"
        )
    except ValueError:
        resolved = expected
    matches = head == resolved or head.startswith(resolved) or resolved.startswith(head)
    return {
        "state": "matches" if matches else "diverged",
        "expected": resolved,
        "actual": head,
        "source": source,
    }


def stale_action(freshness, policy="warn_only"):
    if policy not in {"auto_rebase", "auto_merge_forward", "warn_only", "block"}:
        raise ValueError("未知分支过期策略")
    if freshness["state"] == "fresh":
        return {"kind": "noop"}
    return {
        "kind": {
            "auto_rebase": "rebase",
            "auto_merge_forward": "merge_forward",
            "warn_only": "warn",
            "block": "block",
        }[policy],
        "message": f"Branch {freshness.get('ahead', 0)} ahead, {freshness.get('behind', 0)} behind; missing fixes: "
        + "; ".join(freshness.get("missing_fixes", [])),
    }


async def freshness(cwd, branch, main_ref):
    # Resolve refs before constructing a range; no untrusted option can reach git log.
    revisions = [
        await git(cwd, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        for ref in (branch, main_ref)
    ]
    local, base = revisions
    behind = int(await git(cwd, "rev-list", "--count", f"{local}..{base}"))
    ahead = int(await git(cwd, "rev-list", "--count", f"{base}..{local}"))
    subjects = (await git(cwd, "log", "--format=%s", f"{local}..{base}")).splitlines()
    return {
        "state": "fresh" if not behind else "diverged" if ahead else "stale",
        "ahead": ahead,
        "behind": behind,
        "missing_fixes": [s for s in subjects if "fix" in s.lower()],
    }
