from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent
from .config import DATA, ROOT, ConfigStore
from .db import Database
from .extension_api import router as extension_router
from .extensions import Extensions
from .oauth import OAuth
from .providers import Provider
from .rag import RagService
from .tools import Tools
from .tunnel import Tunnel


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=16000)
    permission: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=12)


def create_app(data: Path = DATA, root: Path = ROOT):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = ConfigStore(data, root)
        from .config_layers import activate

        activate(app.state.store)
        app.state.db = Database(data / "app.sqlite")
        app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=12), trust_env=False)
        app.state.tunnel = Tunnel()
        app.state.oauth = OAuth(app.state.store, app.state.client)
        app.state.provider = Provider(app.state.client, app.state.oauth, app.state.store)
        app.state.rag = RagService(
            app.state.db, app.state.provider, app.state.client, app.state.tunnel
        )
        app.state.extensions = Extensions(app.state.db, app.state.store, app.state.client)
        app.state.extensions.oauth = app.state.oauth
        app.state.agent = Agent(
            app.state.db,
            app.state.provider,
            Tools(app.state.rag, app.state.extensions),
            app.state.store,
        )
        app.state.extensions.jobs.agent = app.state.agent
        await app.state.extensions.start()
        yield
        await app.state.extensions.close()
        await app.state.tunnel.close()
        await app.state.client.aclose()

    app = FastAPI(
        title="MyClaude API",
        root_path=os.environ.get("MYCLAUDE_ROOT_PATH", "").rstrip("/"),
        version="0.4.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    hosts = ["127.0.0.1", "localhost", "testserver"]
    hosts.extend(
        x.strip() for x in os.environ.get("MYCLAUDE_ALLOWED_HOSTS", "").split(",") if x.strip()
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    origins = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    origins.extend(
        x.strip().rstrip("/")
        for x in os.environ.get("MYCLAUDE_ALLOWED_ORIGINS", "").split(",")
        if x.strip()
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        path = request.scope["path"]
        prefix = request.scope.get("root_path", "")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        if path.startswith("/api") and origin and origin not in origins:
            return JSONResponse({"detail": "请求来源不在允许列表中"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "请求参数不合法，请检查必填字段和数值范围"}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error(request, error):
        message = app.state.store.redact(str(error))[:600]
        return JSONResponse({"detail": message or "操作失败，请查看运行记录"}, status_code=400)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.4.0", "service": "myclaude"}

    @app.get("/api/overview")
    async def overview():
        db, cfg = app.state.db, app.state.store.current
        totals = db.all(
            "SELECT COUNT(*) AS runs,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens FROM runs"
        )[0]
        return {
            **totals,
            "sessions": db.all("SELECT COUNT(*) AS n FROM sessions")[0]["n"],
            "documents": db.all("SELECT COUNT(*) AS n FROM documents")[0]["n"],
            "chunks": db.all("SELECT COUNT(*) AS n FROM chunks")[0]["n"],
            "active": len(app.state.agent.active),
            "model": cfg.model,
            "configured": bool(cfg.api_key),
            "workspace": cfg.workspace,
            "tools": app.state.agent.tools.definitions("workspace-write", cfg.rag_enabled),
        }

    @app.get("/api/settings")
    async def settings():
        return app.state.store.public()

    @app.put("/api/settings")
    async def save_settings(changes: dict):
        if app.state.agent.active or app.state.rag.lock.locked():
            raise HTTPException(409, "请等待当前运行或入库结束后再修改配置")
        try:
            app.state.store.update(changes)
        except ValidationError as error:
            raise HTTPException(
                422,
                "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()),
            )
        except ValueError as error:
            raise HTTPException(422, str(error))
        await app.state.tunnel.close()
        return app.state.store.public()

    @app.post("/api/connections/{kind}")
    async def connection(kind: Literal["llm", "embedding", "qdrant"]):
        cfg = app.state.store.current.model_copy(deep=True)
        if kind == "qdrant":
            return await app.state.rag.health(cfg)
        if kind == "embedding":
            vectors = await app.state.provider.embed(cfg, ["connection test"])
            return {
                "ok": True,
                "model": cfg.embedding_model,
                "dimension": len(vectors[0]),
            }
        cfg.max_tokens = 64
        text = ""
        async for item in app.state.provider.stream(
            cfg, [{"role": "user", "content": "Reply OK."}], [], "Be concise."
        ):
            if item["type"] == "delta":
                text += item["text"]
        return {"ok": True, "model": cfg.model, "reply": text[:100]}

    @app.get("/api/sessions")
    async def sessions():
        return app.state.db.all(
            "SELECT id,title,created_at,updated_at FROM sessions ORDER BY updated_at DESC"
        )

    @app.post("/api/sessions")
    async def create_session():
        session = app.state.db.create_session()
        app.state.extensions.jobs.put(
            "session_metadata", session["id"], {"workspace_root": app.state.store.current.workspace}
        )
        return session

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        row = app.state.db.session(session_id)
        if row is None:
            raise HTTPException(404, "会话不存在")
        return row

    @app.post("/api/chat")
    async def chat(body: ChatRequest):
        agent = app.state.agent
        session = app.state.db.session(body.session_id)
        if not session:
            raise HTTPException(404, "会话不存在")
        if body.session_id in agent.active:
            raise HTTPException(409, "该会话正在运行")
        if not body.message.strip():
            raise HTTPException(422, "消息不能为空")
        cfg = app.state.store.current.model_copy(deep=True)
        agent.active.add(body.session_id)

        async def events():
            try:
                async for event in agent.run(session, body.message, body.permission, cfg):
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                agent.active.discard(body.session_id)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs")
    async def runs():
        return app.state.db.all(
            "SELECT r.*,s.title FROM runs r LEFT JOIN sessions s ON s.id=r.session_id ORDER BY r.started_at DESC LIMIT 100"
        )

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        rows = app.state.db.all("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            raise HTTPException(404, "运行记录不存在")
        return {**rows[0], "events": app.state.db.events(run_id)}

    @app.get("/api/documents")
    async def documents():
        return app.state.db.all(
            "SELECT id,name,created_at,model,dimension,chunks,backend FROM documents ORDER BY created_at DESC"
        )

    @app.post("/api/documents")
    async def upload(file: UploadFile = File(...)):
        name = file.filename or "document.txt"
        if Path(name).suffix.lower() not in {
            ".txt",
            ".md",
            ".py",
            ".rs",
            ".js",
            ".ts",
            ".vue",
            ".html",
            ".css",
        }:
            raise HTTPException(
                422,
                "支持 UTF-8 文本、Markdown 和代码文件；不支持 PDF、二进制或凭据文件",
            )
        raw = await file.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "文件超过 2 MB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "请将文件保存为 UTF-8 编码")
        return await app.state.rag.ingest(app.state.store.current.model_copy(deep=True), name, text)

    @app.post("/api/retrieve")
    async def retrieve(body: QueryRequest):
        return await app.state.rag.query(
            app.state.store.current.model_copy(deep=True), body.query, body.top_k
        )

    app.include_router(extension_router)

    dist = root / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        if (dist / "index.html").exists():
            return FileResponse(dist / "index.html")
        return JSONResponse(
            {
                "message": "后端已启动。请在 frontend 执行 npm run build 或启动 Vite。",
                "docs": "/api/docs",
            }
        )

    return app


app = create_app()
