"""Frozen upstream Bash heuristics for diagnostics, never an authorization boundary.

Actual execution still requires the tool permission gate and configured sandbox.
Compound shell syntax cannot be securely authorized by a first-command classifier.
"""

import json
import re
from pathlib import Path

RULES = json.loads(
    Path(__file__).with_name("specs").joinpath("bash_validation.json").read_text(encoding="utf-8")
)


def first_command(command):
    rest = command.strip()
    while match := re.match(r"[A-Za-z0-9_]+=", rest):
        value = rest[match.end() :].lstrip()
        if not value:
            return ""
        if value[0] in "\"'":
            end = next(
                (
                    i + 1
                    for i in range(1, len(value))
                    if value[i] == value[0] and value[i - 1] != "\\"
                ),
                len(value),
            )
            while end < len(value) and not value[end].isspace():
                end += 1
        else:
            end = next((i for i, ch in enumerate(value) if ch.isspace()), len(value))
        rest = value[end:].lstrip()
    return rest.split()[0] if rest.split() else ""


def git_subcommand(command):
    return next((part for part in command.split()[1:] if not part.startswith("-")), None)


def classify_command(command):
    first = first_command(command)
    if first in RULES["SEMANTIC_READ_ONLY_COMMANDS"]:
        return "Write" if first == "sed" and " -i" in command else "ReadOnly"
    if first in RULES["ALWAYS_DESTRUCTIVE_COMMANDS"] or first == "rm":
        return "Destructive"
    for category, label in (
        ("WRITE_COMMANDS", "Write"),
        ("NETWORK_COMMANDS", "Network"),
        ("PROCESS_COMMANDS", "ProcessManagement"),
        ("PACKAGE_COMMANDS", "PackageManagement"),
        ("SYSTEM_ADMIN_COMMANDS", "SystemAdmin"),
    ):
        if first in RULES[category]:
            return label
    if first == "git":
        return (
            "ReadOnly" if git_subcommand(command) in RULES["GIT_READ_ONLY_SUBCOMMANDS"] else "Write"
        )
    return "Unknown"


def validate_read_only(command, mode):
    if mode != "read-only":
        return {"decision": "Allow"}
    first = first_command(command)
    for category, subject in (
        ("WRITE_COMMANDS", "the filesystem"),
        ("STATE_MODIFYING_COMMANDS", "system state"),
    ):
        if first in RULES[category]:
            return {
                "decision": "Block",
                "reason": f"Command '{first}' modifies {subject} and is not allowed in read-only mode",
            }
    if first == "sudo":
        parts = command.split()
        inner = next((p for p in parts[parts.index("sudo") + 1 :] if not p.startswith("-")), None)
        if inner:
            result = validate_read_only(command[command.index(inner) :], mode)
            if result["decision"] != "Allow":
                return result
    if ">" in command:
        return {
            "decision": "Block",
            "reason": "Command contains write redirection '>' which is not allowed in read-only mode",
        }
    if first == "git":
        sub = git_subcommand(command)
        if sub and sub not in RULES["GIT_READ_ONLY_SUBCOMMANDS"]:
            return {
                "decision": "Block",
                "reason": f"Git subcommand '{sub}' modifies repository state and is not allowed in read-only mode",
            }
    return {"decision": "Allow"}


def validate_command(command, mode, workspace):
    result = validate_read_only(command, mode)
    if result["decision"] != "Allow":
        return result
    first = first_command(command)
    if (
        mode == "workspace-write"
        and first in {*RULES["WRITE_COMMANDS"], *RULES["STATE_MODIFYING_COMMANDS"]}
        and any(
            path in command
            for path in (
                "/etc/",
                "/usr/",
                "/var/",
                "/boot/",
                "/sys/",
                "/proc/",
                "/dev/",
                "/sbin/",
                "/lib/",
                "/opt/",
            )
        )
    ):
        return {
            "decision": "Warn",
            "message": "Command appears to target files outside the workspace — requires elevated permission",
        }
    if first == "sed" and mode == "read-only" and " -i" in command:
        return {
            "decision": "Block",
            "reason": "sed -i (in-place editing) is not allowed in read-only mode",
        }
    for pattern, message in RULES["DESTRUCTIVE_PATTERNS"]:
        if pattern in command:
            return {"decision": "Warn", "message": "Destructive command detected: " + message}
    if first in RULES["ALWAYS_DESTRUCTIVE_COMMANDS"]:
        return {
            "decision": "Warn",
            "message": f"Command '{first}' is inherently destructive and may cause data loss",
        }
    if all(part in command for part in ("rm ", "-r", "-f")):
        return {
            "decision": "Warn",
            "message": "Recursive forced deletion detected — verify the target path is correct",
        }
    if "../" in command and str(workspace) not in command:
        return {
            "decision": "Warn",
            "message": "Command contains directory traversal pattern '../' — verify the target path resolves within the workspace",
        }
    if "~/" in command or "$HOME" in command:
        return {
            "decision": "Warn",
            "message": "Command references home directory — verify it stays within the workspace scope",
        }
    return {"decision": "Allow"}
