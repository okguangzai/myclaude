import asyncio
import io
import json
import sqlite3
import subprocess
import time
import wave
import zipfile
from pathlib import Path

import httpx
import pytest
from app.agent import Agent
from app.commands import Commands
from app.config import ConfigStore, Settings
from app.db import Database
from app.extensions import Extensions
from app.feature_ops import migrate, migration_status
from app.features import FEATURE_COMMANDS
from app.ide_extension import package
from app.media import Media
from app.providers import Provider
from app.request_policy import Limiter
from app.tools import Tools
from PIL import Image


@pytest.fixture
async def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("import os\nvalue = 42\n")
    store = ConfigStore(tmp_path / "data", tmp_path)
    store.update(
        {
            "workspace": str(workspace),
            "rag_enabled": False,
            "api_key": "synthetic-test-secret",
            "base_url": "https://model.example",
            "provider_retries": 0,
        }
    )

    def handler(request):
        if request.url.path == "/embeddings":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ext = Extensions(Database(store.data / "app.sqlite"), store, client)
        ext.jobs.agent = Agent(ext.db, Provider(client, store=store), Tools(None, ext), store)
        yield ext
        await ext.close()


async def command(ext, text, session=None, permission="workspace-write"):
    return await Commands(ext).execute(text, session, permission)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["vim", "voice", "advisor", "notifications"])
async def test_preferences_persist_and_permissions(runtime, name):
    with pytest.raises(PermissionError):
        await command(runtime, f"/{name} on", permission="read-only")
    await command(runtime, f"/{name} on")
    assert runtime.features.prefs()[name]
    ext2 = Extensions(Database(runtime.db.path, recover=False), runtime.store, runtime.client)
    assert ext2.features.prefs()[name]
    await command(runtime, f"/{name} off")
    assert not runtime.features.prefs()[name]
    await ext2.close()


@pytest.mark.asyncio
async def test_keybindings_validation(runtime):
    keys = {"send": "Ctrl+Enter", "new": "Alt+n", "stop": "Escape", "focus": "Alt+i"}
    await command(runtime, "/keybindings " + json.dumps(keys))
    assert runtime.features.prefs()["keybindings"] == keys
    for bad in ({}, {**keys, "send": "Escape"}, {**keys, "focus": "eval(foo)"}):
        with pytest.raises(ValueError):
            await command(runtime, "/keybindings " + json.dumps(bad))
    await command(runtime, "/keybindings reset")
    assert runtime.features.prefs()["keybindings"]["send"] == "Enter"


@pytest.mark.asyncio
async def test_alias_and_macro_execute_with_permission_boundaries(runtime):
    sid = runtime.db.create_session()["id"]
    await command(runtime, "/alias set details /status")
    assert (await command(runtime, "/details"))["command"] == "status"
    await command(runtime, "/alias set a /b")
    with pytest.raises(ValueError, match="循环"):
        await command(runtime, "/alias set b /a")
    assert "b" not in runtime.features.aliases()
    with pytest.raises(ValueError):
        await command(runtime, "/alias set help /status")
    await command(runtime, "/macro record check", sid)
    await command(runtime, "/details", sid)
    await command(runtime, "/feedback macro-output", sid)
    await command(runtime, "/macro stop", sid)
    assert len(runtime.jobs.record("macro", "check")["commands"]) == 2
    result = await command(runtime, "/macro run check", sid)
    assert len(result["result"]["steps"]) == 2
    assert len(runtime.jobs.records("feedback")) == 2
    with pytest.raises(PermissionError):
        await command(runtime, "/macro run check", sid, "read-only")
    runtime.jobs.put(
        "macro",
        "elevate",
        {"commands": ["/permissions danger-full-access", "/feedback should-not-run"]},
    )
    before = len(runtime.jobs.records("feedback"))
    with pytest.raises((PermissionError, ValueError)):
        await command(runtime, "/macro run elevate", sid, "read-only")
    assert len(runtime.jobs.records("feedback")) == before


@pytest.mark.asyncio
async def test_share_snapshot_redaction_revoke_and_expiry(runtime):
    sid = runtime.db.create_session("shared")["id"]
    runtime.db.save_messages(
        sid,
        [
            {"role": "user", "content": "api_key=synthetic-test-secret password=hello"},
            {"role": "assistant", "content": "answer"},
            {"role": "tool", "content": "private tool result"},
        ],
    )
    row = (await command(runtime, "/share create 1", sid))["result"]
    saved = runtime.features.read_share(row["id"])
    assert "synthetic-test-secret" not in json.dumps(saved)
    assert "password=hello" not in json.dumps(saved)
    assert len(saved["messages"]) == 2
    runtime.db.save_messages(sid, [])
    assert len(runtime.features.read_share(row["id"])["messages"]) == 2
    await command(runtime, "/share revoke " + row["id"])
    with pytest.raises(ValueError):
        runtime.features.read_share(row["id"])
    row = runtime.jobs.record("share", row["id"])
    key = row.pop("id")
    runtime.jobs.put("share", key, {**row, "revoked": False, "expires_at": time.time() - 1})
    with pytest.raises(ValueError):
        runtime.features.read_share(key)


@pytest.mark.asyncio
async def test_stickers_feedback_and_actual_benchmark(runtime):
    row = (await command(runtime, '/stickers {"name":"test","items":["😀","OK"]}'))["result"]
    assert len(runtime.features.stickers()) == 2
    await command(runtime, "/stickers remove " + row["id"])
    assert len(runtime.features.stickers()) == 1
    with pytest.raises(ValueError):
        await command(runtime, "/stickers remove builtin")
    await command(runtime, "/feedback reproducible problem")
    assert runtime.jobs.records("feedback")[0]["status"] == "open"
    bench = (await command(runtime, "/benchmark 2"))["result"]
    assert bench["successful"] == 2
    assert all(
        r["first_token_seconds"] is not None and r["usage"]["output_tokens"] == 1
        for r in bench["samples"]
    )


@pytest.mark.asyncio
async def test_autofix_real_ruff_preview_apply_and_backup(runtime):
    path = Path(runtime.store.current.workspace) / "sample.py"
    before = path.read_text()
    preview = (await command(runtime, "/autofix preview sample.py", permission="read-only"))[
        "result"
    ]
    assert preview["changes"] and path.read_text() == before
    with pytest.raises(PermissionError):
        await command(runtime, "/autofix apply sample.py", permission="read-only")
    fixed = (await command(runtime, "/autofix apply sample.py"))["result"]
    assert "import os" not in path.read_text()
    assert json.loads((Path(fixed["backup"]) / "originals.json").read_text())["sample.py"] == before
    with pytest.raises(ValueError):
        await command(runtime, "/autofix apply ../outside.py")


def test_migrations_backup_idempotency_and_integrity(tmp_path):
    db = Database(tmp_path / "app.sqlite")
    sid = db.create_session("preserved")["id"]
    assert migration_status(db)["pending"] == 2
    result = migrate(db, tmp_path, True)
    assert result["applied_now"] == [1, 2]
    with sqlite3.connect(result["backup"]) as con:
        assert (
            con.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            == "preserved"
        )
    assert migrate(db, tmp_path, True)["applied_now"] == []
    with db.connect() as con:
        con.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
    with pytest.raises(ValueError, match="校验和"):
        migrate(db, tmp_path, True)


@pytest.mark.asyncio
async def test_privacy_history_traces_and_cloud_gate(runtime):
    await command(runtime, '/privacy-settings {"store_history":false,"store_traces":false}')
    sid = runtime.db.create_session()["id"]
    events = [
        e
        async for e in runtime.jobs.agent.run(
            runtime.db.session(sid), "private text", "read-only", runtime.store.current
        )
    ]
    assert events[-1]["status"] == "completed"
    assert runtime.db.session(sid)["messages"] == []
    assert [r["kind"] for r in runtime.db.all("SELECT kind FROM events")] == ["run_end"]
    assert not list((runtime.store.data / "traces").glob("*"))
    await command(runtime, '/privacy-settings {"allow_cloud":false}')
    with pytest.raises(ValueError, match="禁止云"):
        async for _ in runtime.jobs.agent.provider.stream(runtime.store.current, [], [], ""):
            pass
    with pytest.raises(ValueError, match="禁止云"):
        await runtime.jobs.agent.provider.embed(runtime.store.current, ["x"])


@pytest.mark.asyncio
async def test_advisor_enforces_read_only_and_notifications(runtime):
    await command(runtime, "/advisor on")
    sid = runtime.db.create_session()["id"]
    events = [
        e
        async for e in runtime.jobs.agent.run(
            runtime.db.session(sid), "hello", "danger-full-access", runtime.store.current
        )
    ]
    assert events[0]["permission"] == "read-only"
    assert runtime.db.all("SELECT permission FROM runs")[0]["permission"] == "read-only"
    assert runtime.jobs.records("notification")[0]["status"] == "completed"
    await command(runtime, "/notifications off")
    _ = [
        e
        async for e in runtime.jobs.agent.run(
            runtime.db.session(sid), "again", "danger-full-access", runtime.store.current
        )
    ]
    assert len(runtime.jobs.records("notification")) == 1


@pytest.mark.asyncio
async def test_limiter_cancellation_concurrency_and_rpm():
    limiter = Limiter(window=0.05)
    cfg = Settings(rate_limit_rpm=2, rate_limit_concurrency=1)
    entered = asyncio.Event()

    async def hold():
        async with limiter.slot(cfg):
            entered.set()
            await asyncio.sleep(10)

    first = asyncio.create_task(hold())
    await entered.wait()
    waiting = asyncio.create_task(hold())
    await asyncio.sleep(0.005)
    assert limiter.active == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert limiter.active == 0
    async with limiter.slot(cfg):
        pass
    start = time.monotonic()
    async with limiter.slot(cfg):
        assert time.monotonic() - start > 0.02


def png():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_images_real_provider_payloads_and_validation(runtime):
    media = Media(runtime)
    item = media.image(png(), "test.png")
    assert media.get(item["id"])[0]["mime"] == "image/jpeg"
    assert media.image_content(item["id"], "anthropic")["source"]["media_type"] == "image/jpeg"
    for bad in (b"fake", b"x" * (5 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            media.image(bad)
    with pytest.raises(ValueError):
        media.get("../../settings.json")
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"red"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = Provider(client, store=runtime.store)
        _ = [
            e
            async for e in provider.stream(
                runtime.store.current,
                [{"role": "user", "content": "color?", "attachments": [item["id"]]}],
                [],
                "",
            )
        ]
    content = requests[0]["messages"][1]["content"]
    assert content[0]["text"] == "color?"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert (
        Provider.anthropic_messages(
            [{"role": "user", "content": [media.image_content(item["id"], "anthropic")]}]
        )[0]["content"][0]["type"]
        == "image"
    )


def wav(rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * rate)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_speech_nls_protocol_and_dashscope_fallback(runtime):
    runtime.store.update(
        {
            "runtime_environment": {
                "ASR_URL": "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1",
                "ASR_TOKEN": "fake-token",
                "ASR_APPKEY": "fake-app",
                "TTS_URL": "wss://nls-gateway-cn-beijing.aliyuncs.com/ws/v1",
                "TTS_TOKEN": "fake-token",
                "TTS_APPKEY": "fake-app",
            }
        }
    )
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("asr"):
            assert request.headers["X-NLS-Token"] == "fake-token"
            assert request.content.startswith(b"RIFF")
            return httpx.Response(200, json={"status": 20000000, "result": "你好"})
        assert json.loads(request.content)["token"] == "fake-token"
        return httpx.Response(200, content=wav(), headers={"content-type": "audio/wav"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runtime.client = client
        media = Media(runtime)
        assert await media.transcribe(wav()) == "你好"
        audio = await media.synthesize("a" * 301)
        assert len(seen) == 3
        assert media.get(audio["id"])[1].is_file()
    runtime.store.update({"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})

    def fallback(request):
        if "nls-gateway" in request.url.host:
            return httpx.Response(400, json={"status": 40000001})
        if request.method == "GET":
            assert request.url.scheme == "https" and "authorization" not in request.headers
            return httpx.Response(200, content=wav(24000))
        body = json.loads(request.content)
        if body["model"] == "qwen3-asr-flash":
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "fallback transcript"}}]}
            )
        return httpx.Response(
            200,
            json={
                "output": {
                    "audio": {
                        "url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.wav"
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(fallback)) as client:
        runtime.client = client
        media = Media(runtime)
        assert await media.transcribe(wav()) == "fallback transcript"
        value = await media.synthesize("hello")
        assert media.wav(media.get(value["id"])[1].read_bytes())[0] == 16000
        assert runtime.jobs.record("speech_status", "TTS")["provider"] == "dashscope"


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", ["powershell", "bash", "zsh", "fish"])
async def test_terminal_scripts_contain_real_launcher(runtime, shell):
    response = await command(runtime, "/terminal-setup " + shell)
    path = Path(response["result"]["path"])
    assert path.is_file() and "-m app.cli" in path.read_text()
    assert ("-l permission" if shell == "fish" else "--permission") in path.read_text()


def test_installable_ide_package():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        manifest = json.loads(z.read("extension/package.json"))
        assert manifest["main"] == "./extension.js"
        source = z.read("extension/extension.js").decode()
        assert "context.secrets.store" in source and "permission:'read-only'" in source
        assert "extension.vsixmanifest" in z.namelist()


@pytest.mark.asyncio
async def test_all_feature_commands_have_real_dispatch(runtime):
    sid = runtime.db.create_session()["id"]
    arguments = {
        "vim": "status",
        "voice": "status",
        "advisor": "status",
        "notifications": "status",
        "image": "sample.png",
        "listen": "sample.wav",
        "speak": "hello",
        "changelog": "",
        "release-notes": "",
        "autofix": "preview sample.py",
        "migrate": "plan",
        "benchmark": "1",
    }
    # Media cloud adapters and Git operations are tested separately; every remaining entry must dispatch.
    exercised = set()
    for name in sorted(FEATURE_COMMANDS - {"listen", "speak", "changelog", "release-notes"}):
        if name == "image":
            (Path(runtime.store.current.workspace) / "sample.png").write_bytes(png())
        response = await command(
            runtime, "/" + name + (" " + arguments[name] if arguments.get(name) else ""), sid
        )
        assert response["kind"] in {"result", "ui", "attachment", "draft"}
        assert "upstream-placeholder" not in json.dumps(response)
        exercised.add(name)
    assert len(exercised) == 21


@pytest.mark.asyncio
async def test_managed_upgrade_uses_deployment_configuration(tmp_path, monkeypatch):
    from app.feature_ops import upgrade

    monkeypatch.delenv("MYCLAUDE_PIPELINE_URL", raising=False)
    assert (await upgrade(tmp_path))["pipeline_url"] is None
    monkeypatch.setenv("MYCLAUDE_PIPELINE_URL", "https://ci.example.test/project/pipelines")
    assert (await upgrade(tmp_path))["pipeline_url"] == "https://ci.example.test/project/pipelines"
    for invalid in ("javascript:alert(1)", "https://user:secret@ci.example.test", "https://["):
        monkeypatch.setenv("MYCLAUDE_PIPELINE_URL", invalid)
        assert (await upgrade(tmp_path))["pipeline_url"] is None


@pytest.mark.asyncio
async def test_release_notes_and_upgrade_fast_forward(tmp_path, monkeypatch):
    from app import feature_ops
    from app.feature_ops import release_notes, upgrade

    remote, source, checkout = (tmp_path / n for n in ("remote.git", "source", "checkout"))

    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], stderr=subprocess.STDOUT, text=True
        ).strip()

    remote.mkdir()
    source.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "file.txt").write_text("before")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    git(tmp_path, "clone", str(remote), str(checkout))
    old = git(checkout, "rev-parse", "HEAD")
    (source / "file.txt").write_text("after")
    git(source, "commit", "-am", "feature added")
    git(source, "push", "origin", "main")
    assert (await upgrade(checkout))["update_available"]
    with pytest.raises(PermissionError):
        await upgrade(checkout, True, "workspace-write")
    (checkout / "local.txt").write_text("preserve")
    with pytest.raises(ValueError, match="未提交"):
        await upgrade(checkout, True, "danger-full-access")
    (checkout / "local.txt").unlink()
    actual = feature_ops.process
    installs = []

    async def installer(args, *pos, **kw):
        if args[0] != "git":
            installs.append(args)
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return await actual(args, *pos, **kw)

    monkeypatch.setattr(feature_ops, "process", installer)
    result = await upgrade(checkout, True, "danger-full-access")
    assert result["applied"] and (checkout / "file.txt").read_text() == "after"
    assert len(installs) == 3
    notes = await release_notes(checkout, old)
    assert len(notes["commits"]) == 1 and "feature added" in notes["markdown"]
    with pytest.raises(ValueError):
        await release_notes(checkout, "--all")


def test_feature_http_roundtrip_and_csrf(tmp_path):
    from app.main import create_app
    from fastapi.testclient import TestClient

    (tmp_path / "workspace").mkdir()
    with TestClient(
        create_app(tmp_path / "data", tmp_path), raise_server_exceptions=False
    ) as client:
        assert client.get("/api/features").status_code == 200
        assert client.put("/api/features/preferences", json={"vim": True}).json()["vim"]
        assert client.put("/api/features/preferences", json={"vim": "yes"}).status_code == 400
        assert (
            client.put(
                "/api/features/preferences",
                json={"vim": False},
                headers={"Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        uploaded = client.post(
            "/api/features/media/image", files={"file": ("test.png", png(), "image/png")}
        ).json()
        response = client.get("/api/features/media/" + uploaded["id"])
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/chat",
                json={
                    "session_id": "missing",
                    "message": "hello",
                    "attachments": [uploaded["id"]] * 5,
                },
            ).status_code
            == 422
        )
        sid = client.post("/api/sessions").json()["id"]
        response = client.post(
            "/api/commands",
            json={"text": "/share", "session_id": sid, "permission": "workspace-write"},
        ).json()
        key = response["result"]["id"]
        assert client.get("/api/features/shares/" + key).status_code == 200
        assert client.delete("/api/features/shares/" + key).status_code == 200
        assert client.get("/api/features/shares/" + key).status_code == 404
        assert client.get("/api/features/ide/extension").content.startswith(b"PK")
        assert (
            client.post(
                "/api/features/speech/listen", files={"file": ("bad.wav", b"bad")}
            ).status_code
            == 400
        )


@pytest.mark.asyncio
async def test_macro_recording_limits_and_removal(runtime):
    await command(runtime, "/macro record bounded")
    with pytest.raises(ValueError, match="人工"):
        await command(runtime, "/screenshot")
    runtime.jobs.put("macro", "bounded", {"commands": ["/status"] * 32})
    with pytest.raises(ValueError, match="32"):
        await command(runtime, "/feedback must-not-save")
    assert runtime.jobs.records("feedback") == []
    await command(runtime, "/macro remove bounded")
    assert runtime.jobs.records("macro_recording") == []


@pytest.mark.asyncio
async def test_macro_passes_images_to_real_agent(runtime):
    (Path(runtime.store.current.workspace) / "picture.png").write_bytes(png())
    sid = runtime.db.create_session()["id"]
    runtime.jobs.put(
        "macro", "image-review", {"commands": ["/image picture.png", "/explain 图片颜色"]}
    )
    result = await command(runtime, "/macro run image-review", sid)
    assert result["result"]["steps"][-1]["response"]["result"]["status"] == "completed"
    assert runtime.db.session(sid)["messages"][0]["attachments"]
