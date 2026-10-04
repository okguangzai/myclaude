"""Recovery recipes and bounded attempt ledger; executors supply actual step outcomes.

The upstream recovery module simulates steps. Evaluation retains that mode explicitly;
it never reports a Git operation or worker restart as having happened on the machine.
"""

from copy import deepcopy
from datetime import datetime, timezone

RECIPES = {
    "trust_prompt_unresolved": (["accept_trust_prompt"], "alert_human"),
    "prompt_misdelivery": (["redirect_prompt_to_agent"], "alert_human"),
    "stale_branch": (["rebase_branch", "clean_build"], "alert_human"),
    "compile_red_cross_crate": (["clean_build"], "alert_human"),
    "mcp_handshake_failure": ([{"retry_mcp_handshake": {"timeout": 5000}}], "abort"),
    "partial_plugin_startup": (
        [{"restart_plugin": {"name": "stalled"}}, {"retry_mcp_handshake": {"timeout": 3000}}],
        "log_and_continue",
    ),
    "provider_failure": (["restart_worker"], "alert_human"),
}


def recipe_for(scenario):
    if scenario not in RECIPES:
        raise ValueError("未知恢复场景")
    steps, policy = RECIPES[scenario]
    return {
        "scenario": scenario,
        "steps": deepcopy(steps),
        "max_attempts": 1,
        "escalation_policy": policy,
    }


class RecoveryContext:
    def __init__(self):
        self.ledger = {}
        self.events = []

    def status(self, scenario):
        recipe_for(scenario)
        row = self.ledger.get(scenario)
        return {
            "scenario": scenario,
            "attempted": bool(row),
            "state": row["state"] if row else None,
            "attempt_count": row["attempt_count"] if row else 0,
            "retry_limit": row["retry_limit"] if row else None,
            "attempts_remaining": row["attempts_remaining"] if row else None,
            "escalation_reason": row["escalation_reason"] if row else None,
        }

    async def attempt(self, scenario, executor):
        recipe = recipe_for(scenario)
        row = self.ledger.setdefault(
            scenario,
            {
                "recipe_id": scenario,
                "attempt_type": "automatic",
                "trigger": scenario,
                "attempt_count": 0,
                "retry_limit": 1,
                "attempts_remaining": 1,
                "state": "queued",
                "started_at": None,
                "finished_at": None,
                "command_results": [],
                "result": None,
                "last_failure_summary": None,
                "escalation_reason": None,
            },
        )
        if row["attempt_count"] >= 1:
            result = {
                "escalation_required": {
                    "reason": f"max recovery attempts (1) exceeded for {scenario}"
                }
            }
            row.update(
                state="exhausted",
                result=result,
                escalation_reason=result["escalation_required"]["reason"],
            )
        else:
            row.update(
                attempt_count=1,
                attempts_remaining=0,
                state="running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            recovered = []
            for index, step in enumerate(recipe["steps"]):
                try:
                    outcome = await executor(deepcopy(step))
                    if outcome is False:
                        raise ValueError("executor returned false")
                except Exception as exc:
                    row["command_results"].append(
                        {"command": step, "status": "failed", "result": str(exc)[:1000]}
                    )
                    row["last_failure_summary"] = str(exc)[:1000]
                    result = (
                        {
                            "partial_recovery": {
                                "recovered": recovered,
                                "remaining": recipe["steps"][index:],
                            }
                        }
                        if recovered
                        else {
                            "escalation_required": {
                                "reason": f"recovery failed at first step for {scenario}"
                            }
                        }
                    )
                    row["state"] = "failed" if recovered else "exhausted"
                    break
                recovered.append(step)
                row["command_results"].append(
                    {"command": step, "status": "succeeded", "result": outcome}
                )
            else:
                result = {"recovered": {"steps_taken": len(recovered)}}
                row["state"] = "succeeded"
            row["result"] = result
            row["escalation_reason"] = result.get("escalation_required", {}).get("reason")
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.events.extend(
            [
                {"recovery_attempted": {"scenario": scenario, "recipe": recipe, "result": result}},
                "recovery_succeeded"
                if "recovered" in result
                else "recovery_failed"
                if "partial_recovery" in result
                else "escalated",
            ]
        )
        return deepcopy(result)


async def simulate(scenario, fail_at_step=None, attempts=1):
    if not 1 <= attempts <= 3:
        raise ValueError("仿真 attempts 范围 1–3")
    context = RecoveryContext()
    index = 0

    async def execute(step):
        nonlocal index
        current = index
        index += 1
        return current != fail_at_step

    for _ in range(attempts):
        await context.attempt(scenario, execute)
    return {
        "mode": "simulation",
        "side_effects_executed": False,
        "status": context.status(scenario),
        "ledger": context.ledger,
        "events": context.events,
    }
