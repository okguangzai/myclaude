import sqlite3

import httpx
import pytest
from app.rag_service import EmbedConfig, chunk_text, create_app, ingest, query, stats
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_standalone_rag_incremental_multiroot_delete_and_http(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
        (root / "note.md").write_text("hello RAG service content 中文" * 80, encoding="utf-8")
    path = tmp_path / "index.sqlite"
    cfg = EmbedConfig("mock", "mock://", "mock-embedding")
    async with httpx.AsyncClient() as client:
        assert (await query(path, client, cfg, "hello"))["phase"] == "1-sqlite-no-db"
        result = await ingest(roots, path, cfg, client)
        assert result["files_indexed"] == 2 and result["embeddings_written"] >= 4
        assert (await ingest(roots, path, cfg, client))["embeddings_written"] == 0
        found = await query(path, client, cfg, "RAG service", 1)
        assert found["phase"] == "1-sqlite" and len(found["hits"]) == 1
        assert ":note.md" in found["hits"][0]["path"]
        before = stats(path)["chunks"]
        (roots[1] / "note.md").unlink()
        (roots[0] / "note.md").write_text("replacement")
        await ingest(roots, path, cfg, client)
        assert stats(path)["chunks"] == 1 < before
        # The actual Rust schema and little-endian float vectors can be read directly.
        with sqlite3.connect(path) as conn:
            dim, blob = conn.execute("SELECT dim,vec FROM embeddings").fetchone()
            assert dim == 16 and len(blob) == dim * 4
    with TestClient(create_app(path, cfg)) as web:
        assert web.get("/health").text == "ok"
        assert web.get("/v1/stats").json()["chunks"] == 1
        assert web.post("/v1/query", json={"query": "replacement"}).json()["hits"]
        assert "/v1/query" in web.get("/").text
    assert chunk_text("1234567890", 6, 2) == ["123456", "567890"]
    assert chunk_text("hi", 0) == []


@pytest.mark.asyncio
async def test_failed_embedding_preserves_previous_index_and_dimension_errors(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    doc = root / "text.md"
    doc.write_text("old-content")
    path = tmp_path / "index.sqlite"
    cfg = EmbedConfig("mock", "mock://", "mock-embedding")
    async with httpx.AsyncClient() as client:
        await ingest([root], path, cfg, client)
    doc.write_text("new-content")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        with pytest.raises(ValueError, match="HTTP 503"):
            await ingest([root], path, EmbedConfig("key", "https://example.test", "model"), client)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "old-content"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"embedding": [1, 2]}]})
        )
    ) as client:
        with pytest.raises(ValueError, match="dimension mismatch"):
            await query(path, client, EmbedConfig("key", "https://example.test", "model"), "query")


@pytest.mark.asyncio
async def test_qdrant_query_and_sqlite_fallback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "x.txt").write_text("local copy")
    cfg = EmbedConfig("mock", "mock://", "mock")
    path = tmp_path / "idx.sqlite"
    async with httpx.AsyncClient() as client:
        await ingest([root], path, cfg, client)
    cfg.qdrant_url = "http://qdrant.test"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        assert (await query(path, client, cfg, "q"))["phase"] == "1-sqlite"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {"payload": {"path": "repo:x.txt", "text": "remote copy"}, "score": 0.8}
                        ]
                    }
                },
            )
        )
    ) as client:
        result = await query(path, client, cfg, "q")
        assert result["phase"] == "2-qdrant" and result["hits"][0]["snippet"] == "remote copy"
