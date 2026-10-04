from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .commands import Commands
from .execution import RunContext, current
from .extensions import CATALOG

Permission = Literal["read-only", "workspace-write", "danger-full-access"]


class ToolCall(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    permission: Permission = "read-only"


class CommandCall(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    session_id: str | None = None
    permission: Permission = "read-only"


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=16000)
    description: str = Field(default="后台 Agent", max_length=200)
    permission: Permission = "read-only"


class TeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tasks: list[str] = Field(min_length=1, max_length=4)
    permission: Permission = "read-only"


class ScheduleRequest(TaskRequest):
    schedule: str


router = APIRouter(prefix="/api")


@router.delete("/sessions/{key}")
async def delete_session(request: Request, key: str):
    return request.app.state.extensions.sessions.delete(key)


@router.get("/plugins/registry")
async def plugin_registry(request: Request):
    return request.app.state.extensions.plugins.registry()


@router.post("/plugins/sync")
async def plugin_sync(request: Request):
    return await request.app.state.extensions.plugins.sync()


@router.post("/runtime/config-layers")
async def config_layers(request: Request, body: dict):
    from .config_layers import inspect

    if body.get("apply") and request.app.state.agent.active:
        raise ValueError("任务运行中不能导入配置")
    return inspect(request.app.state.store, body.get("config_home"), body.get("apply", False))


@router.post("/runtime/evaluate/{kind}")
async def runtime_evaluate(
    request: Request,
    kind: Literal[
        "policy",
        "green",
        "collisions",
        "packet",
        "recovery",
        "report",
        "projection",
        "branch",
        "usage",
        "lanes",
        "completion",
        "bash",
    ],
    body: dict,
):
    from .orchestration import branch_collisions, evaluate_policy, green_contract
    from .task_packet import validate_packet

    if kind == "completion":
        from .orchestration import detect_lane_completion, evaluate_completed_lane

        context = detect_lane_completion(body["output"], body["test_green"], body["has_pushed"])
        return {
            "context": context,
            "evaluation": evaluate_completed_lane(context) if context else None,
            "side_effects_executed": False,
        }

    if kind == "bash":
        from .bash_validation import classify_command, validate_command

        return {
            "intent": classify_command(body["command"]),
            **validate_command(
                body["command"],
                body.get("permission", "read-only"),
                request.app.state.store.current.workspace,
            ),
        }
    if kind == "policy":
        return evaluate_policy(body["rules"], body["context"])
    if kind == "lanes":
        from .lanes import dedupe_commits, filter_events, reconcile

        events = filter_events(
            body["events"],
            body.get("provenance"),
            body.get("environment"),
            body.get("min_confidence"),
        )
        return {"events": dedupe_commits(events), **reconcile(events)}
    if kind == "green":
        return green_contract(body["contract"], body["evidence"])
    if kind == "collisions":
        return branch_collisions(body["intents"])
    if kind == "usage":
        from .usage import summarize

        return summarize(
            body["messages"],
            body.get("model"),
            body.get("rates"),
            body.get("source_compatibility", False),
        )
    if kind == "recovery":
        from .recovery import simulate

        return await simulate(body["scenario"], body.get("fail_at_step"), body.get("attempts", 1))
    if kind in {"report", "projection"}:
        from .reports import canonicalize, project

        return (
            canonicalize(body)
            if kind == "report"
            else project(body["report"], body["capabilities"], body.get("view", "default"))
        )
    if kind == "branch":
        from .git_context import freshness, stale_action

        value = await freshness(
            request.app.state.store.current.workspace, body["branch"], body["main_ref"]
        )
        return {
            "freshness": value,
            "action": stale_action(value, body.get("policy", "warn_only")),
            "side_effects_executed": False,
        }
    return validate_packet(body)


@router.post("/sessions/import")
async def import_session(request: Request, body: dict):
    return request.app.state.extensions.sessions.import_text(
        body["text"], body.get("allow_workspace_mismatch", False)
    )


@router.get("/sessions/{key}/export")
async def export_session(request: Request, key: str, format: Literal["json", "jsonl"] = "json"):
    return {"text": request.app.state.extensions.sessions.export(key, format)}


@router.post("/sessions/{key}/fork")
async def fork_session(request: Request, key: str, body: dict):
    return request.app.state.extensions.sessions.fork(key, body.get("name"))


@router.post("/sessions/{key}/compact")
async def compact_session(request: Request, key: str, body: dict):
    if key in request.app.state.agent.active:
        raise HTTPException(409, "会话正在运行")
    return request.app.state.extensions.sessions.compact(
        key, body.get("preserve", 4), body.get("mode", "standard")
    )


@router.post("/control/shutdown")
async def shutdown(request: Request):
    callback = getattr(request.app.state, "request_shutdown", None)
    if not callback:
        raise HTTPException(409, "此服务由外部 uvicorn 管理，请使用启动终端停止")
    callback()
    return {"stopping": True}


@router.post("/tasks")
async def create_task(request: Request, body: TaskRequest):
    e = request.app.state.extensions
    return e.jobs.agent_job(body.prompt, body.permission, e.store.current, body.description)


@router.post("/teams")
async def create_team(request: Request, body: TeamRequest):
    from .db import now, uid

    e = request.app.state.extensions
    created = []
    try:
        for prompt in body.tasks:
            if not prompt.strip() or len(prompt) > 16000:
                raise ValueError("团队任务长度应为 1–16000")
            created.append(
                e.jobs.agent_job(prompt, body.permission, e.store.current, body.name)["id"]
            )
    except BaseException:
        for key in created:
            await e.jobs.stop(key)
        raise
    return e.jobs.put("team", uid(), {"name": body.name, "tasks": created, "created_at": now()})


@router.post("/schedules")
async def create_schedule(request: Request, body: ScheduleRequest):
    e = request.app.state.extensions
    return e.jobs.cron(body.schedule, body.prompt, body.permission, body.description)


@router.get("/extensions")
async def extensions(request: Request):
    e = request.app.state.extensions
    return {
        "mcp": e.mcp.list(),
        "plugins": e.plugins.list(),
        "workers": e.jobs.records("worker"),
        "teams": e.jobs.records("team"),
        "schedules": e.jobs.records("cron"),
        "notifications": e.jobs.records("notification")[-50:],
        "plugin_errors": e.jobs.records("plugin_error"),
    }


@router.get("/catalog")
async def catalog(request: Request):
    return {
        "revision": CATALOG["revision"],
        "tools": CATALOG["tools"],
        "commands": Commands(request.app.state.extensions).catalog(),
    }


@router.post("/commands")
async def command(request: Request, body: CommandCall):
    e = request.app.state.extensions
    result = await Commands(e).execute(body.text, body.session_id, body.permission)
    return json.loads(e.store.redact(json.dumps(result, ensure_ascii=False)))


@router.post("/tools/execute")
async def tool(request: Request, body: ToolCall):
    e = request.app.state.extensions
    session = e.db.create_session("工具实验室 · " + body.name)
    run_id = e.db.start_run(session["id"], "[local-tool]", body.permission)
    token = current.set(
        RunContext(session_id=session["id"], run_id=run_id, permission=body.permission)
    )
    status, error = "completed", None
    try:
        e.db.event(
            run_id,
            "tool_start",
            {
                "name": body.name,
                "arguments": e.store.redact(json.dumps(body.arguments, ensure_ascii=False)),
            },
        )
        result = await e.execute(
            e.store.current.model_copy(deep=True), body.permission, body.name, body.arguments
        )
        e.db.event(run_id, "tool_result", {"name": body.name, "output": result, "is_error": False})
        return result
    except asyncio.CancelledError:
        status, error = "cancelled", "工具请求取消"
        raise
    except Exception as exc:
        status, error = "failed", e.store.redact(str(exc))[:1500]
        e.db.event(
            run_id, "tool_result", {"name": body.name, "output": {"error": error}, "is_error": True}
        )
        raise
    finally:
        e.db.finish_run(run_id, status, 0, {}, error)
        e.db.event(run_id, "run_end", {"status": status, "error": error})
        current.reset(token)


@router.put("/mcp")
async def mcp_config(request: Request, body: dict):
    return await request.app.state.extensions.mcp.configure(body)


@router.post("/mcp/{name}/{action}")
async def mcp_action(
    request: Request,
    name: str,
    action: Literal["discover", "disconnect", "remove", "prompts", "prompt"],
    body: dict,
):
    e = request.app.state.extensions
    if action in {"disconnect", "remove"}:
        await e.mcp.disconnect(name)
        if action == "remove":
            e.jobs.remove("mcp", name)
        return {"ok": True}
    permission = body.get("permission", "read-only")
    if permission not in {"read-only", "workspace-write", "danger-full-access"}:
        raise HTTPException(422, "权限不合法")
    if action == "discover":
        return await e.mcp.discover(name, permission)
    if action == "prompts":
        return await e.mcp.call(name, permission, "list_prompts")
    return await e.mcp.call(
        name, permission, "get_prompt", name=body["name"], arguments=body.get("arguments", {})
    )


@router.post("/plugins/install")
async def plugin_install(request: Request, body: dict):
    return await request.app.state.extensions.plugins.install_source(body["path"])


@router.post("/plugins/{name}/update")
async def plugin_update(request: Request, name: str, body: dict):
    return await request.app.state.extensions.plugins.update(name, body.get("source"))


@router.get("/assets/{kind}")
async def assets_list(request: Request, kind: str):
    return request.app.state.extensions.assets.list(kind)


@router.post("/skills/install")
async def skill_install(request: Request, body: dict):
    return request.app.state.extensions.assets.install_skill(body["source"])


@router.post("/skills/{name}/remove")
async def skill_remove(request: Request, name: str):
    return request.app.state.extensions.assets.uninstall_skill(name)


@router.post("/agents/create")
async def agent_create(request: Request, body: dict):
    return request.app.state.extensions.assets.create_agent(**body)


@router.get("/oauth")
async def oauth_status(request: Request):
    return request.app.state.oauth.status()


@router.post("/oauth/start")
async def oauth_start(request: Request, body: dict):
    return request.app.state.oauth.begin(body)


@router.get("/oauth/callback")
async def oauth_callback(request: Request):
    from fastapi.responses import HTMLResponse

    await request.app.state.oauth.complete(request.url.query)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>授权完成</title><p>授权完成，可以关闭本页并返回 MyClaude 设置。</p>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/oauth/complete")
async def oauth_complete(request: Request, body: dict):
    return await request.app.state.oauth.complete(body["callback"])


@router.post("/oauth/logout")
async def oauth_logout(request: Request):
    return await request.app.state.oauth.logout()


@router.post("/oauth/refresh")
async def oauth_refresh(request: Request):
    await request.app.state.oauth.access_token(force_refresh=True)
    return request.app.state.oauth.status()


@router.post("/plugins/{name}/{action}")
async def plugin_action(
    request: Request, name: str, action: Literal["enable", "disable", "remove"]
):
    plugins = request.app.state.extensions.plugins
    return (
        await plugins.uninstall(name)
        if action == "remove"
        else await plugins.enable(name, action == "enable")
    )


@router.get("/jobs")
async def jobs(request: Request):
    return request.app.state.extensions.jobs.list()


@router.get("/runtime/lane-board")
async def lane_board(request: Request, stalled_after: int = 60):
    if stalled_after < 1:
        raise ValueError("stalled_after must be positive")
    return request.app.state.extensions.jobs.lane_board(stalled_after=stalled_after)


@router.get("/runtime/mcp-health")
async def mcp_health(request: Request):
    from .lifecycle import degraded_report

    extensions = request.app.state.extensions
    servers = extensions.mcp.health()
    available = [tool["name"] for tools in extensions.mcp.cache.values() for tool in tools]
    return {"servers": servers, **degraded_report(servers, available)}


@router.post("/workers/{key}/startup-timeout")
async def worker_startup_timeout(request: Request, key: str, body: dict):
    extensions = request.app.state.extensions
    async with extensions.worker_lock(key):
        return await _worker_startup_timeout(request, key, body)


async def _worker_startup_timeout(request: Request, key: str, body: dict):
    from .worker import startup_timeout

    jobs = request.app.state.extensions.jobs
    worker = jobs.record("worker", key)
    if not worker:
        raise HTTPException(404, "Worker 不存在")
    if worker.get("job_id") and jobs.get(worker["job_id"])["status"] in {"queued", "running"}:
        raise HTTPException(409, "实际任务仍在执行；请先检查或取消任务")
    worker.pop("id", None)
    for name in ("transport_healthy", "mcp_healthy"):
        if not isinstance(body.get(name), bool):
            raise ValueError(name + " must be an observed boolean")
    startup_timeout(
        worker,
        str(body.get("pane_command", ""))[:2000],
        body["transport_healthy"],
        body["mcp_healthy"],
    )
    return jobs.put("worker", key, worker)


@router.get("/jobs/{key}")
async def job(request: Request, key: str):
    return request.app.state.extensions.jobs.get(key)


@router.post("/jobs/{key}/stop")
async def stop_job(request: Request, key: str):
    return await request.app.state.extensions.jobs.stop(key)


@router.get("/questions")
async def questions(request: Request, session_id: str | None = None):
    return [
        {"id": key, **{k: v for k, v in question.items() if k != "future"}}
        for key, question in request.app.state.extensions.questions.items()
        if session_id is None or question["session_id"] == session_id
    ]


@router.post("/questions/{key}/answer")
async def answer(request: Request, key: str, body: dict):
    question = request.app.state.extensions.questions.get(key)
    if not question or question["future"].done():
        raise HTTPException(404, "问题已结束")
    text = body.get("answer")
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise HTTPException(422, "回答长度应为 1–8000")
    question["future"].set_result(text)
    return {"ok": True}
