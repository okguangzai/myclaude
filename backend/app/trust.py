"""Trust resolver for observed worker gates; denials always precede allowlists."""

import fnmatch
from pathlib import Path

TRUST_CUES = (
    "do you trust the files in this folder",
    "trust the files in this folder",
    "trust this folder",
    "allow and continue",
    "yes, proceed",
)
MANUAL_CUES = ("yes, i trust", "i trust this", "trusted manually", "approval granted")


def pattern_matches(pattern, path):
    pattern, path = (
        pattern.strip().replace("\\", "/").replace("//", "/"),
        path.strip().replace("\\", "/").replace("//", "/"),
    )
    if pattern == path:
        return True
    if (
        "*" not in pattern
        and "?" not in pattern
        and (path == pattern or path.startswith(pattern.rstrip("/") + "/"))
    ):
        return True
    if pattern.endswith("/*"):
        root = pattern[:-2]
        return path == root or path.startswith(root + "/")
    if pattern.endswith("*") and "/*/" not in pattern:
        root = pattern.rstrip("*")
        return path.startswith(root) and not path[len(root) :].startswith("/")
    if any(pattern in part for part in path.split("/")):
        return True
    return (
        fnmatch.fnmatchcase(path, pattern)
        if "?" in pattern or "/*/" in pattern or pattern.startswith("*/")
        else False
    )


def policy(cwd, allowlisted=(), denied=(), worktree=None):
    if any(Path(cwd).resolve().is_relative_to(Path(root).resolve()) for root in denied):
        return "deny"
    for entry in allowlisted:
        entry = {"pattern": entry} if isinstance(entry, str) else entry
        if pattern_matches(entry["pattern"], cwd) and (
            not entry.get("worktree_pattern")
            or worktree
            and pattern_matches(entry["worktree_pattern"], worktree)
        ):
            return "auto_trust"
    return "require_approval"


def resolve(cwd, screen, allowlisted=(), denied=(), worktree=None):
    lower = screen.lower()
    if not any(cue in lower for cue in TRUST_CUES):
        return {"required": False, "policy": None, "events": []}
    selected = policy(cwd, allowlisted, denied, worktree)
    events = [{"type": "trust_required", "cwd": cwd, "repo": Path(cwd).name, "worktree": worktree}]
    if selected == "deny":
        events.append(
            {"type": "trust_denied", "cwd": cwd, "reason": "cwd matches denied trust root"}
        )
    elif selected == "auto_trust" or any(cue in lower for cue in MANUAL_CUES):
        events.append(
            {
                "type": "trust_resolved",
                "cwd": cwd,
                "policy": selected,
                "resolution": "auto_allowlisted" if selected == "auto_trust" else "manual_approval",
            }
        )
    return {"required": True, "policy": selected, "events": events}
