from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path

import httpx

from .config import Settings
from .db import Database, now, uid
from .providers import Provider, check_response
from .tunnel import Tunnel


def chunks(text: str, size=900, overlap=120) -> list[str]:
    return [
        text[i : i + size]
        for i in range(0, len(text), size - overlap)
        if text[i : i + size].strip()
    ]


def namespace(cfg: Settings) -> str:
    # Include the embedding identity. An equal dimension does not mean equal semantics.
    return hashlib.sha256(f"{cfg.embedding_base_url}|{cfg.embedding_model}".encode()).hexdigest()[
        :16
    ]


class RagService:
    def __init__(
        self,
        db: Database,
        provider: Provider,
        client: httpx.AsyncClient,
        tunnel: Tunnel,
    ):
        self.db, self.provider, self.client, self.tunnel = db, provider, client, tunnel
        self.lock = asyncio.Lock()

    async def qrequest(self, cfg, method, path, **kwargs):
        await self.tunnel.ensure(cfg)
        headers = {"api-key": cfg.qdrant_api_key} if cfg.qdrant_api_key else {}
        response = await self.client.request(
            method, cfg.qdrant_url + path, headers=headers, **kwargs
        )
        return response

    async def ensure_collection(self, cfg: Settings, dimension: int):
        path = "/collections/" + cfg.qdrant_collection
        response = await self.qrequest(cfg, "GET", path)
        if response.status_code == 404:
            response = await self.qrequest(
                cfg,
                "PUT",
                path,
                json={"vectors": {"size": dimension, "distance": "Cosine"}},
            )
            await check_response(response, "Qdrant")
        else:
            await check_response(response, "Qdrant")
            vectors = response.json()["result"]["config"]["params"]["vectors"]
            if vectors.get("size") != dimension:
                raise ValueError("Qdrant 集合维度不匹配，请使用新的 myclaude_ 集合")

    async def ingest(self, cfg: Settings, name: str, text: str):
        if not text.strip():
            raise ValueError("文档内容不能为空")
        if len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("每份文档最多 2 MB")
        pieces = chunks(text)
        if len(pieces) > 256:
            raise ValueError("学习版单次最多 256 块，请拆分文档")
        async with self.lock:
            vectors = []
            for i in range(0, len(pieces), 10):
                vectors.extend(await self.provider.embed(cfg, pieces[i : i + 10]))
            dimension, doc_id = len(vectors[0]), uid()
            point_ids = [uid() for _ in pieces]
            backend = "sqlite"
            if cfg.qdrant_url:
                await self.ensure_collection(cfg, dimension)
                points = [
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": {
                            "document_id": doc_id,
                            "name": name,
                            "text": text,
                            "ordinal": i,
                            "namespace": namespace(cfg),
                        },
                    }
                    for i, (point_id, vector, text) in enumerate(zip(point_ids, vectors, pieces))
                ]
                response = await self.qrequest(
                    cfg,
                    "PUT",
                    f"/collections/{cfg.qdrant_collection}/points?wait=true",
                    json={"points": points},
                )
                await check_response(response, "Qdrant 写入")
                backend = "qdrant"
            # Publish the document only once vectors were durably written.
            with self.db.connect() as c:
                c.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        Path(name).name[:180],
                        text,
                        now(),
                        cfg.embedding_model,
                        dimension,
                        namespace(cfg),
                        len(pieces),
                        backend,
                    ),
                )
                c.executemany(
                    "INSERT INTO chunks VALUES(?,?,?,?,?)",
                    [
                        (p, doc_id, i, t, json.dumps(v))
                        for i, (p, t, v) in enumerate(zip(point_ids, pieces, vectors))
                    ],
                )
            return {
                "id": doc_id,
                "name": name,
                "chunks": len(pieces),
                "dimension": dimension,
                "backend": backend,
            }

    async def query(self, cfg: Settings, query: str, top_k=5):
        if not cfg.rag_enabled:
            raise ValueError("知识库功能尚未启用")
        top_k = min(max(int(top_k), 1), 12)
        docs = self.db.all("SELECT id FROM documents WHERE namespace=?", (namespace(cfg),))
        if not docs:
            return {
                "hits": [],
                "backend": "empty",
                "warning": "当前向量模型下还没有文档，请先上传",
            }
        vector = (await self.provider.embed(cfg, [query]))[0]
        warning = None
        try:
            response = await self.qrequest(
                cfg,
                "POST",
                f"/collections/{cfg.qdrant_collection}/points/query",
                json={
                    "query": vector,
                    "limit": top_k,
                    "with_payload": True,
                    "filter": {
                        "must": [
                            {"key": "namespace", "match": {"value": namespace(cfg)}},
                            {
                                "key": "document_id",
                                "match": {"any": [d["id"] for d in docs]},
                            },
                        ]
                    },
                },
            )
            await check_response(response, "Qdrant 检索")
            hits = [
                {"id": p["id"], "score": p["score"], **p["payload"]}
                for p in response.json()["result"]["points"]
            ]
            if hits:
                return {"hits": hits, "backend": "qdrant", "warning": None}
            warning = "远程集合没有匹配点，已使用本地向量索引；检查集合配置"
        except Exception:
            # Fallback is visible in both UI and tool trace, never disguised as Qdrant.
            warning = "Qdrant 暂不可用，已使用本地向量索引"
        rows = self.db.all(
            "SELECT c.*,d.name FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.namespace=? AND d.dimension=?",
            (namespace(cfg), len(vector)),
        )
        qnorm = math.sqrt(sum(v * v for v in vector)) or 1
        hits = []
        for row in rows:
            v = json.loads(row["vector"])
            score = sum(a * b for a, b in zip(v, vector)) / (
                qnorm * (math.sqrt(sum(a * a for a in v)) or 1)
            )
            hits.append(
                {
                    "id": row["id"],
                    "document_id": row["document_id"],
                    "name": row["name"],
                    "text": row["text"],
                    "ordinal": row["ordinal"],
                    "score": score,
                }
            )
        return {
            "hits": sorted(hits, key=lambda h: h["score"], reverse=True)[:top_k],
            "backend": "sqlite-fallback",
            "warning": warning,
        }

    async def health(self, cfg):
        response = await self.qrequest(cfg, "GET", "/collections")
        await check_response(response, "Qdrant")
        return {
            "ok": True,
            "collections": len(response.json()["result"]["collections"]),
            "collection": cfg.qdrant_collection,
        }
