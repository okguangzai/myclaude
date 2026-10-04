from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .media import MAX_UPLOAD, Media

router = APIRouter(prefix="/api/features")


@router.get("")
async def status(request: Request):
    ext = request.app.state.extensions
    cfg = ext.store.current
    env = Media(ext).env()
    from urllib.parse import urlsplit

    dashscope_configured = bool(cfg.api_key) and urlsplit(cfg.base_url).hostname in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
    }
    return {
        "preferences": ext.features.prefs(),
        "shares": ext.features.shares(),
        "aliases": ext.features.aliases(),
        "macros": ext.jobs.records("macro"),
        "feedback": ext.jobs.records("feedback"),
        "stickers": ext.features.stickers(),
        "notifications": ext.jobs.records("notification")[-100:],
        "privacy": {
            k: getattr(cfg, "privacy_" + k)
            for k in ("store_history", "store_traces", "allow_cloud")
        },
        "rate_limit": {"rpm": cfg.rate_limit_rpm, "concurrency": cfg.rate_limit_concurrency},
        "vision_model": cfg.vision_model,
        "speech_provider": cfg.speech_provider,
        "speech_status": ext.jobs.records("speech_status"),
        "speech": {
            service: (cfg.speech_provider != "nls" and dashscope_configured)
            or (
                cfg.speech_provider != "dashscope"
                and all(env.get(service + "_" + k) for k in ("URL", "APPKEY", "TOKEN"))
            )
            for service in ("ASR", "TTS")
        },
    }


@router.put("/preferences")
async def preferences(request: Request, body: dict):
    return request.app.state.extensions.features.preferences(body)


@router.get("/shares/{token}")
async def share(request: Request, token: str):
    try:
        return request.app.state.extensions.features.read_share(token)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/shares/{token}")
async def revoke_share(request: Request, token: str):
    return request.app.state.extensions.features.revoke_share(token)


@router.post("/notifications/read")
async def mark_read(request: Request):
    jobs = request.app.state.extensions.jobs
    for row in jobs.records("notification"):
        key = row.pop("id")
        jobs.put("notification", key, {**row, "read": True})
    return {"ok": True}


@router.post("/feedback/{key}/resolve")
async def resolve_feedback(request: Request, key: str):
    jobs = request.app.state.extensions.jobs
    row = jobs.record("feedback", key)
    if not row:
        raise HTTPException(404, "反馈不存在")
    row.pop("id")
    return jobs.put("feedback", key, {**row, "status": "resolved"})


@router.post("/media/image")
async def image(request: Request, file: UploadFile = File(...)):
    return Media(request.app.state.extensions).image(
        await file.read(MAX_UPLOAD + 1), file.filename or "image"
    )


@router.get("/media/{key}")
async def media(request: Request, key: str):
    row, path = Media(request.app.state.extensions).get(key)
    return FileResponse(
        path,
        media_type=row["mime"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/speech/listen")
async def listen(request: Request, file: UploadFile = File(...)):
    return {
        "text": await Media(request.app.state.extensions).transcribe(
            await file.read(MAX_UPLOAD + 1)
        )
    }


class Speech(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


@router.post("/speech/speak")
async def speak(request: Request, body: Speech):
    return await Media(request.app.state.extensions).synthesize(body.text)


@router.get("/ide/extension")
async def ide_extension():
    from .ide_extension import package

    return Response(
        package(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="myclaude-0.8.0.vsix"'},
    )
