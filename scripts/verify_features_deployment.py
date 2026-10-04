"""Production smoke for the v0.8 extensions. Credentials stay in the ignored access file."""

import argparse
import io
import json
import secrets
from pathlib import Path

import httpx
from PIL import Image


def verify(access):
    report = {}
    prefix = "verify-" + secrets.token_hex(4)
    with httpx.Client(
        base_url=access["url"].rstrip("/") + "/",
        auth=(access["username"], access["password"]),
        timeout=180,
        trust_env=False,
    ) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response

        def command(text, sid=None):
            return request(
                "POST",
                "api/commands",
                json={"text": text, "session_id": sid, "permission": "workspace-write"},
            ).json()

        report["health"] = request("GET", "api/health").json()
        assert report["health"]["version"] == "0.8.0"
        catalog = request("GET", "api/catalog").json()
        assert len(catalog["tools"]) == 55 and len(catalog["commands"]) == 140
        assert all(c["python_status"] != "upstream-placeholder" for c in catalog["commands"])
        report["tools"], report["commands"], report["placeholders"] = 55, 140, 0
        state = request("GET", "api/features").json()
        assert set(state["preferences"]["keybindings"]) == {"send", "new", "stop", "focus"}
        session = request("POST", "api/sessions").json()
        report["session_id"] = session["id"]
        image = Image.new("RGB", (160, 100), "red")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        attachment = request(
            "POST",
            "api/features/media/image",
            files={"file": ("verification-red.png", buffer.getvalue(), "image/png")},
        ).json()
        response = request(
            "POST",
            "api/chat",
            json={
                "session_id": session["id"],
                "message": "这张图片是什么颜色？只回答颜色，不调用工具。",
                "attachments": [attachment["id"]],
                "permission": "read-only",
            },
        )
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["status"] == "completed"
        answer = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        assert "红" in answer or "red" in answer.lower()
        transcript = request("GET", "api/sessions/" + session["id"]).json()
        assert transcript["messages"][0]["attachments"] == [attachment["id"]]
        report["vision"] = {"ok": True, "answer": answer, "image_id": attachment["id"]}
        share = command("/share create 1", session["id"])["result"]
        assert request("GET", "api/features/shares/" + share["id"]).json()["messages"]
        command("/share revoke " + share["id"])
        assert client.get("api/features/shares/" + share["id"]).status_code == 404
        report["share_create_read_revoke"] = True
        try:
            command(f"/alias set {prefix} /status")
            assert command("/" + prefix)["command"] == "status"
            command("/macro record " + prefix, session["id"])
            command("/" + prefix, session["id"])
            command("/macro stop", session["id"])
            replay = command("/macro run " + prefix, session["id"])["result"]
            assert (
                len(replay["steps"]) == 1 and replay["steps"][0]["response"]["command"] == "status"
            )
            report["alias_and_macro_replay"] = True
        finally:
            command("/macro stop", session["id"])
            command("/macro remove " + prefix, session["id"])
            command("/alias remove " + prefix)
        report["migration"] = command("/migrate apply")["result"]
        assert report["migration"]["pending"] == 0
        report["benchmark"] = command("/benchmark 1")["result"]
        assert report["benchmark"]["successful"] == 1
        audio = request(
            "POST", "api/features/speech/speak", json={"text": "你好，这是语音功能测试。"}
        ).json()
        binary = request("GET", "api/features/media/" + audio["id"]).content
        assert binary.startswith(b"RIFF")
        recognized = request(
            "POST",
            "api/features/speech/listen",
            files={"file": ("roundtrip.wav", binary, "audio/wav")},
        ).json()["text"]
        assert "语音" in recognized
        report["speech"] = {
            "tts_bytes": len(binary),
            "asr_text": recognized,
            "status": request("GET", "api/features").json()["speech_status"],
        }
        assert request("GET", "api/features/ide/extension").content.startswith(b"PK")
        assert request("GET", "manifest.webmanifest").json()["display"] == "standalone"
        assert "fetch" in request("GET", "sw.js").text
        assert command("/upgrade check")["result"]["mode"] == "managed-ci"
        report["ide_desktop_upgrade"] = True
        assert command("/release-notes")["result"]["markdown"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=Path(".local/deploy/access.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/features-v08-final.json"))
    args = parser.parse_args()
    result = verify(json.loads(args.credentials.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "version": result["health"]["version"],
                "commands": result["commands"],
                "vision": True,
                "speech": True,
                "macro": True,
                "share": True,
            }
        )
    )


if __name__ == "__main__":
    main()
