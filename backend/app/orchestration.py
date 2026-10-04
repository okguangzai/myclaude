"""Deterministic orchestration contracts from the frozen Rust runtime.

These functions evaluate supplied evidence. They do not perform a merge or invent evidence.
"""

from __future__ import annotations

import copy
import time
from itertools import combinations

GREEN_LEVELS = ["targeted_tests", "package", "workspace", "merge_ready"]


def branch_collisions(intents):
    found = set()
    for left, right in combinations(intents, 2):
        if left["branch"] != right["branch"]:
            continue
        for a in left.get("modules", []):
            for b in right.get("modules", []):
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    found.add(
                        (
                            left["branch"],
                            b if a.startswith(b + "/") else a,
                            left["laneId"],
                            right["laneId"],
                        )
                    )
    return [
        {"branch": branch, "module": module, "laneIds": [left, right]}
        for branch, module, left, right in sorted(found)
    ]


def green_contract(contract, evidence):
    required, observed = contract["required_level"], evidence.get("observed_level")
    if required not in GREEN_LEVELS or (observed is not None and observed not in GREEN_LEVELS):
        raise ValueError("unknown green level")
    missing = []
    if observed is None or GREEN_LEVELS.index(observed) < GREEN_LEVELS.index(required):
        missing.append("required_level")
    checks = {
        "test_command_provenance": any(
            c.get("exit_code") == 0 and c.get("command", "").strip()
            for c in evidence.get("test_commands", [])
        ),
        "base_branch_freshness": evidence.get("base_branch_fresh", False),
        "recovery_attempt_context": evidence.get("recovery_attempt_context_recorded", False),
    }
    for requirement in contract.get("requirements", []):
        if requirement not in {*checks, "required_level"}:
            raise ValueError("unknown green requirement")
        if requirement in checks and not checks[requirement]:
            missing.append(requirement)
    flakes = (
        [f for f in evidence.get("known_flakes", []) if f.get("blocks_green")]
        if contract.get("block_known_flakes")
        else []
    )
    result = {
        "outcome": "unsatisfied" if missing or flakes else "satisfied",
        "required_level": required,
        "observed_level": observed,
    }
    if missing or flakes:
        result.update(missing=missing, blocking_flakes=flakes)
    return result


def condition_matches(condition, context):
    kind = condition if isinstance(condition, str) else condition["kind"]
    c = context
    if kind in {"and", "or"}:
        return (all if kind == "and" else any)(
            condition_matches(x, c) for x in condition["conditions"]
        )
    if kind == "green_at":
        return (
            bool(c.get("green_contract_satisfied"))
            and c.get("green_level", 0) >= condition["level"]
        )
    if kind == "timed_out":
        return c.get("branch_freshness", 0) >= condition["duration"]
    values = {
        "stale_branch": c.get("branch_freshness", 0) >= 3600,
        "startup_blocked": c.get("blocker") == "startup",
        "lane_completed": c.get("completed", False),
        "lane_reconciled": c.get("reconciled", False),
        "review_passed": c.get("review_status") == "approved",
        "scoped_diff": c.get("diff_scope") == "scoped",
        "retry_available": c.get("retry_count", 0) < c.get("retry_limit", 1),
        "rebase_required": c.get("rebase_required", False),
        "stale_cleanup_required": c.get("stale_cleanup_required", False),
        "approval_token_present": c.get("approval_token") is not None,
        "approval_token_missing": c.get("approval_token") is None,
    }
    if kind not in values:
        raise ValueError("unknown policy condition: " + kind)
    return values[kind]


def evaluate_policy(rules, context):
    actions, events = [], []
    mapping = {
        "merge_to_dev": "merge",
        "merge_forward": "merge",
        "recover_once": "retry",
        "retry": "retry",
        "rebase": "rebase",
        "escalate": "escalate",
        "cleanup_stale": "stale_cleanup",
        "cleanup_session": "stale_cleanup",
        "closeout_lane": "closeout",
        "reconcile": "reconcile",
        "notify": "notify",
        "require_approval_token": "approval_required",
        "block": "block",
    }

    def flatten(action):
        action = {"kind": action} if isinstance(action, str) else action
        if action["kind"] == "chain":
            return [nested for item in action["actions"] for nested in flatten(item)]
        if action["kind"] not in mapping:
            raise ValueError("unknown policy action")
        return [action]

    for rule in sorted(rules, key=lambda r: r["priority"]):
        if condition_matches(rule["condition"], context):
            for action in flatten(rule["action"]):
                actions.append(action)
                events.append(
                    {
                        "lane_id": context["lane_id"],
                        "rule_name": rule["name"],
                        "priority": rule["priority"],
                        "kind": mapping[action["kind"]],
                        "explanation": f"rule '{rule['name']}' selected {action['kind']}: {action.get('reason', '')}",
                        "approval_token_id": (context.get("approval_token") or {}).get("token_id"),
                    }
                )
    return {"actions": actions, "events": events}


class ApprovalLedger:
    """Audited one-use scopes; caller controls who may grant, executor cannot self-grant."""

    def __init__(self, jobs):
        self.jobs = jobs

    def insert(self, grant):
        grant = copy.deepcopy(grant)
        grant.setdefault("status", "approval_pending")
        grant.setdefault("uses", 0)
        grant["max_uses"] = max(1, grant.get("max_uses", 1))
        grant.setdefault("delegation_chain", [])
        return self.jobs.put("approval", grant["token"], grant)

    def get(self, token):
        grant = self.jobs.record("approval", token)
        if not grant:
            raise ValueError("no_approval")
        grant.pop("id", None)
        return grant

    def revoke(self, token):
        grant = self.get(token)
        grant["status"] = "approval_revoked"
        return self.insert(grant)

    def verify(self, token, scope, actor, at=None, consume=False):
        grant = self.get(token)
        status = grant["status"]
        if status != "approval_granted":
            raise ValueError(
                "approval_already_consumed" if status == "approval_consumed" else status
            )
        now = time.time() if at is None else at
        if (
            grant.get("expires_at_epoch_seconds") is not None
            and now > grant["expires_at_epoch_seconds"]
        ):
            raise ValueError("approval_expired")
        if grant["uses"] >= grant["max_uses"]:
            raise ValueError("approval_already_consumed")
        if grant["scope"] != scope:
            raise ValueError("approval_scope_mismatch")
        if grant["approved_executor"] != actor:
            raise ValueError("approval_unauthorized_delegate")
        if consume:
            grant["uses"] += 1
            if grant["uses"] >= grant["max_uses"]:
                grant["status"] = "approval_consumed"
            self.insert(grant)
        chain = grant["delegation_chain"] or [
            {"actor": grant["approving_actor"], "reason": "approval granted", "session_id": None}
        ]
        delegated = grant["approving_actor"] != actor
        if delegated and not any(h["actor"] == actor for h in chain):
            chain.append({"actor": actor, "reason": "delegated execution", "session_id": None})
        return {
            "token": token,
            "scope": scope,
            "approving_actor": grant["approving_actor"],
            "executing_actor": actor,
            "status": grant["status"],
            "delegated_execution": delegated,
            "delegation_chain": chain,
            "uses": grant["uses"],
            "max_uses": grant["max_uses"],
        }
