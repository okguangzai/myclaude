"""Standalone workspace index compatible with claw-rag-service's SQLite and HTTP API."""

import argparse
import asyncio
import math
import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from blake3 import blake3
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

SKIP = {".git", "target", "node_modules", "__pycache__", ".claw-rag", ".venv", ".local"}
EXTENSIONS = set(
    "rs md toml txt json yaml yml js ts tsx jsx py go c h cpp hpp cs java kt swift rb php sh ps1 html css sql vue".split()
)
SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT NOT NULL,ordinal INTEGER NOT NULL,text TEXT NOT NULL,UNIQUE(path,ordinal));
CREATE TABLE IF NOT EXISTS embeddings(chunk_id INTEGER PRIMARY KEY,dim INTEGER NOT NULL,vec BLOB NOT NULL,FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,content_hash TEXT NOT NULL,size_bytes INTEGER NOT NULL,mtime_ms INTEGER NOT NULL,indexed_at_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
"""


@contextmanager
def database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    try:
        connection.executescript(SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


@dataclass(repr=False)
class EmbedConfig:
    api_key: str
    base_url: str
    model: str
    qdrant_url: str = ""
    qdrant_key: str = ""
    collection: str = "claw_rag_chunks"

    @classmethod
    def from_env(cls):
        if os.environ.get("CLAW_RAG_MOCK_PROVIDERS") == "1":
            return cls("mock", "mock://", "mock-embedding")
        key = os.environ.get("CLAW_RAG_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        if not key:
            raise ValueError("set CLAW_RAG_OPENAI_API_KEY or OPENAI_API_KEY for embeddings")
        return cls(
            key,
            os.environ.get("CLAW_RAG_EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            os.environ.get("CLAW_RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
            os.environ.get("CLAW_RAG_QDRANT_URL", "").rstrip("/"),
            os.environ.get("CLAW_RAG_QDRANT_API_KEY", ""),
            os.environ.get("CLAW_RAG_QDRANT_COLLECTION", "claw_rag_chunks"),
        )


def chunk_text(text, size=900, overlap=120):
    if size <= 0:
        return []
    step, pieces = size - min(overlap, size - 1), []
    for start in range(0, len(text), step):
        piece = text[start : start + size]
        if piece.strip():
            pieces.append(piece)
        if start + size >= len(text):
            break
    return pieces


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0.0
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


async def embeddings(client, cfg, texts):
    if cfg.base_url.startswith("mock://"):
        vectors = []
        for text in texts:
            vector = [0.0] * 16
            for index, byte in enumerate(text.encode()[:64]):
                vector[index % 16] += byte / 255
            norm = math.sqrt(sum(x * x for x in vector))
            vectors.append([x / norm for x in vector] if norm else vector)
        return vectors
    response = await client.post(
        cfg.base_url + "/embeddings",
        headers={"Authorization": "Bearer " + cfg.api_key},
        json={"model": cfg.model, "input": texts},
    )
    if response.is_error:
        raise ValueError(f"embeddings HTTP {response.status_code}")
    rows = response.json()["data"]
    if all("index" in item for item in rows):
        rows.sort(key=lambda item: item["index"])
    vectors = [item["embedding"] for item in rows]
    if len(vectors) != len(texts) or any(
        not v or any(not math.isfinite(x) for x in v) for v in vectors
    ):
        raise ValueError("invalid embeddings count or values")
    if vectors and any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("embedding dimension mismatch within batch")
    return vectors


def repo_id(workspace):
    return workspace.name + "-" + blake3(str(workspace).encode()).hexdigest()[:8]


async def qrequest(client, cfg, method, suffix, **kwargs):
    from urllib.parse import quote

    response = await client.request(
        method,
        cfg.qdrant_url + "/collections/" + quote(cfg.collection, safe="") + suffix,
        headers={"api-key": cfg.qdrant_key} if cfg.qdrant_key else {},
        **kwargs,
    )
    if response.is_error:
        raise ValueError(f"Qdrant HTTP {response.status_code}")
    return response.json()


async def mirror_file(client, cfg, key, pieces, vectors, ids):
    if not cfg.qdrant_url:
        return
    try:
        result = await qrequest(client, cfg, "GET", "")
        if vectors and result["result"]["config"]["params"]["vectors"]["size"] != len(vectors[0]):
            raise ValueError("Qdrant collection dimension mismatch")
    except ValueError as error:
        if str(error) != "Qdrant HTTP 404":
            raise
        if not vectors:
            return
        await qrequest(
            client,
            cfg,
            "PUT",
            "",
            json={"vectors": {"size": len(vectors[0]), "distance": "Cosine"}},
        )
    # Replace one file; remove obsolete points when a file shrinks.
    await qrequest(
        client,
        cfg,
        "POST",
        "/points/delete?wait=true",
        json={"filter": {"must": [{"key": "path", "match": {"value": key}}]}},
    )
    if not vectors:
        return
    await qrequest(
        client,
        cfg,
        "PUT",
        "/points?wait=true",
        json={
            "points": [
                {"id": index, "vector": vector, "payload": {"path": key, "text": piece}}
                for index, piece, vector in zip(ids, pieces, vectors)
            ]
        },
    )


async def ingest(workspaces, db_path, cfg, client):
    if not workspaces:
        raise ValueError("at least one --workspace is required")
    all_files = {}
    for source in workspaces:
        root = Path(source).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in SKIP and not (Path(directory) / d).is_symlink()
            )
            for name in files:
                path = Path(directory) / name
                if (
                    path.is_symlink()
                    or path.suffix[1:].lower() not in EXTENSIONS
                    or path.stat().st_size > 2 * 1024 * 1024
                ):
                    continue
                if (
                    name.lower() in {"credentials.json", "settings.local.json"}
                    or path.resolve() == Path(db_path).resolve()
                ):
                    continue
                all_files[repo_id(root) + ":" + path.relative_to(root).as_posix()] = path
    stats = {"files_indexed": len(all_files), "chunks_total": 0, "embeddings_written": 0}
    with database(db_path) as conn:
        for key, path in sorted(all_files.items()):
            meta = path.stat()
            try:
                raw = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            identity = (blake3(raw.encode()).hexdigest(), meta.st_size, meta.st_mtime_ns // 1000000)
            old = conn.execute(
                "SELECT content_hash,size_bytes,mtime_ms FROM files WHERE path=?", (key,)
            ).fetchone()
            if old == identity:
                continue
            pieces, vectors = chunk_text(raw), []
            for start in range(0, len(pieces), 16):
                vectors.extend(await embeddings(client, cfg, pieces[start : start + 16]))
            # Fetch all vectors before replacing rows, so a failed API request preserves the index.
            conn.execute("DELETE FROM chunks WHERE path=?", (key,))
            ids = []
            for ordinal, (piece, vector) in enumerate(zip(pieces, vectors)):
                row = conn.execute(
                    "INSERT INTO chunks(path,ordinal,text) VALUES(?,?,?)", (key, ordinal, piece)
                ).lastrowid
                conn.execute(
                    "INSERT INTO embeddings VALUES(?,?,?)",
                    (row, len(vector), struct.pack(f"<{len(vector)}f", *vector)),
                )
                ids.append(row)
            await mirror_file(client, cfg, key, pieces, vectors, ids)
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                (key, *identity, int(time.time() * 1000)),
            )
            stats["chunks_total"] += len(pieces)
            stats["embeddings_written"] += len(vectors)
        # The supplied roots describe the full index, matching the upstream ingest contract.
        for (key,) in conn.execute("SELECT path FROM files").fetchall():
            if key not in all_files:
                if cfg.qdrant_url:
                    await qrequest(
                        client,
                        cfg,
                        "POST",
                        "/points/delete?wait=true",
                        json={"filter": {"must": [{"key": "path", "match": {"value": key}}]}},
                    )
                conn.execute("DELETE FROM chunks WHERE path=?", (key,))
                conn.execute("DELETE FROM files WHERE path=?", (key,))
    return stats


def stats(db_path):
    if not Path(db_path).is_file():
        return {"chunks": 0, "phase": "1-sqlite-no-db"}
    with database(db_path) as conn:
        return {
            "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "phase": "1-sqlite",
        }


async def query(db_path, client, cfg, text, top_k=8):
    if not Path(db_path).is_file():
        return {"hits": [], "phase": "1-sqlite-no-db"}
    vector = (await embeddings(client, cfg, [text]))[0]
    limit = min(64, max(0, top_k))
    if cfg.qdrant_url and limit:
        try:
            result = await qrequest(
                client,
                cfg,
                "POST",
                "/points/query",
                json={"query": vector, "limit": limit, "with_payload": True},
            )
            return {
                "hits": [
                    {
                        "path": item["payload"]["path"],
                        "snippet": snippet(item["payload"].get("text", "")),
                        "score": item["score"],
                    }
                    for item in result["result"]["points"]
                    if item.get("payload", {}).get("path")
                ],
                "phase": "2-qdrant",
            }
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # Upstream explicitly falls back to SQLite if Qdrant fails.
    with database(db_path) as conn:
        rows = conn.execute(
            "SELECT c.path,c.text,e.dim,e.vec FROM chunks c JOIN embeddings e ON e.chunk_id=c.id"
        ).fetchall()
    rows = [r for r in rows if r[2] >= 0 and len(r[3]) == r[2] * 4]
    if not rows:
        return {"hits": [], "phase": "1-sqlite-empty"}
    if any(r[2] != len(vector) for r in rows):
        raise ValueError("embedding dimension mismatch: use the same model as ingest")
    hits = [
        {
            "path": path,
            "snippet": snippet(text),
            "score": cosine(vector, struct.unpack(f"<{dim}f", blob)),
        }
        for path, text, dim, blob in rows
    ]
    hits.sort(key=lambda item: item["score"], reverse=True)
    return {"hits": hits[:limit], "phase": "1-sqlite"}


def snippet(text):
    return text[:480] + ("…" if len(text) > 480 else "")


class Query(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=0)


def create_app(db_path, cfg, client=None):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        app.state.client = client or httpx.AsyncClient(timeout=60, trust_env=False)
        yield
        if client is None:
            await app.state.client.aclose()

    app = FastAPI(title="MyClaude Workspace RAG", lifespan=lifespan)

    @app.get("/health", response_class=PlainTextResponse)
    async def health():
        return "ok"

    @app.get("/v1/stats")
    async def get_stats():
        return await asyncio.to_thread(stats, db_path)

    @app.post("/v1/query")
    async def search(body: Query):
        try:
            return await query(db_path, app.state.client, cfg, body.query, body.top_k)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """<!doctype html><meta charset='utf-8'><title>Workspace RAG</title><style>body{max-width:850px;margin:5rem auto;font:16px system-ui;background:#f6f8fa}input{width:65%;padding:12px}button{padding:12px}pre{white-space:pre-wrap}</style><h1>Workspace RAG</h1><p id='stats'></p><form><input aria-label='查询' placeholder='搜索已索引的项目'><button>搜索</button></form><pre id='result'></pre><script>fetch('/v1/stats').then(r=>r.json()).then(x=>document.querySelector('#stats').textContent=JSON.stringify(x));document.querySelector('form').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/v1/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.querySelector('input').value})});document.querySelector('#result').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.querySelector('#result').textContent=String(e)}};</script>"""

    return app


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(prog="myclaude-rag")
    parser.add_argument("command", choices=["serve", "ingest"], nargs="?", default="serve")
    parser.add_argument(
        "--db", type=Path, default=Path(os.environ.get("CLAW_RAG_DB", ".claw-rag/index.sqlite"))
    )
    parser.add_argument("--workspace", "-w", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    cfg = EmbedConfig.from_env()
    if args.command == "ingest":

        async def run():
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                return await ingest(args.workspace, args.db, cfg, client)

        import json

        print(json.dumps(asyncio.run(run())))
    else:
        import uvicorn

        uvicorn.run(
            create_app(args.db, cfg),
            host=os.environ.get("CLAW_RAG_HOST", "127.0.0.1"),
            port=int(os.environ.get("CLAW_RAG_PORT", "8787")),
        )


if __name__ == "__main__":
    main()
