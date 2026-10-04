import json
import os

import httpx
import pytest
from app.db import Database
from app.process_owner import birth
from app.prompt_cache import PromptCache
from app.sse import data_frames


def test_web_startup_preserves_live_cli_run_and_recovers_dead_owner(tmp_path):
    path = tmp_path / "test.sqlite"
    db = Database(path)
    sid = db.create_session()["id"]
    run = db.start_run(sid, "test", "read-only")
    assert birth(os.getpid())
    second = Database(path, recover=True)
    assert second.all("SELECT status FROM runs WHERE id=?", (run,))[0]["status"] == "running"
    with db.connect() as c:
        c.execute("UPDATE process_owners SET birth='wrong-birth' WHERE id=?", (run,))
    third = Database(path, recover=True)
    assert third.all("SELECT status FROM runs WHERE id=?", (run,))[0]["status"] == "interrupted"


def test_prompt_cache_expiration_diagnostics_and_request_identity(tmp_path):
    cache = PromptCache(tmp_path, "session")
    request = {"model": "test", "system": "same", "tools": [], "messages": []}
    cache.record(request, {"cache_read_input_tokens": 5000}, {"answer": "cached"}, now=100)
    assert cache.lookup(request, now=129) == {"answer": "cached"}
    assert cache.lookup(request, now=130) is None
    result = cache.record(request, {"cache_read_input_tokens": 100}, now=140)
    assert result["cache_break"]["unexpected"]
    cache.record(request, {"cache_read_input_tokens": 5000}, now=150)
    changed = {**request, "messages": [{"role": "user", "content": "new"}]}
    result = cache.record(changed, {"cache_read_input_tokens": 0}, now=160)
    assert not result["cache_break"]["unexpected"]
    assert "message payload changed" in result["cache_break"]["reason"]
    assert PromptCache(tmp_path, "session").stats()["tracked_requests"] == 4
    assert PromptCache(tmp_path, "other").lookup(request, now=100) is None


@pytest.mark.asyncio
async def test_sse_multiline_comments_crlf_and_final_frame():
    raw = (
        ': heartbeat\r\nevent: update\r\ndata: {"first":1,\r\ndata: "second":2}\r\n\r\ndata: [DONE]'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
    ) as client:
        async with client.stream("GET", "http://mock.test") as response:
            frames = [frame async for frame in data_frames(response)]
    assert json.loads(frames[0]) == {"first": 1, "second": 2} and frames[1] == "[DONE]"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content="data: toolarge\n\n"))
    ) as client:
        async with client.stream("GET", "http://mock.test") as response:
            with pytest.raises(ValueError, match="size limit"):
                _ = [frame async for frame in data_frames(response, 3)]
