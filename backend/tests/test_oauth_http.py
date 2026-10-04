"""Real loopback OAuth server: redirect, PKCE exchange, refresh, restart and failure.

This validates protocol I/O, not a cloud vendor's account/client registration.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from app.main import create_app
from app.oauth import code_challenge
from fastapi.testclient import TestClient


@pytest.fixture
def authorization_server():
    state = {"codes": {}, "requests": [], "refresh_status": 200, "deny": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path != "/authorize" or query.get("code_challenge_method") != [
                "S256"
            ]:
                return self.reply(400, {"error": "invalid_request"})
            code = "fixture-code-" + str(len(state["codes"]))
            state["codes"][code] = query
            params = {
                "state": query["state"][0],
                **({"error": "access_denied"} if state["deny"] else {"code": code}),
            }
            self.send_response(302)
            self.send_header("Location", query["redirect_uri"][0] + "?" + urlencode(params))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            payload = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state["requests"].append(payload)
            if payload.get("grant_type") == ["authorization_code"]:
                original = state["codes"].pop(payload["code"][0], None)
                if (
                    not original
                    or any(
                        payload.get(key) != original.get(key)
                        for key in ("client_id", "redirect_uri")
                    )
                    or code_challenge(payload["code_verifier"][0]) != original["code_challenge"][0]
                ):
                    return self.reply(400, {"error": "invalid_grant"})
                return self.reply(
                    200,
                    {
                        "access_token": "loopback-access-one",
                        "refresh_token": "loopback-refresh-one",
                        "expires_in": 0,
                        "scope": "read write",
                    },
                )
            if state["refresh_status"] != 200:
                return self.reply(
                    state["refresh_status"],
                    {"error": "invalid_grant", "detail": "loopback-refresh-one"},
                )
            if payload.get("refresh_token") not in (
                ["loopback-refresh-one"],
                ["loopback-refresh-two"],
            ):
                return self.reply(400, {"error": "invalid_grant"})
            return self.reply(
                200,
                {
                    "access_token": "loopback-access-two",
                    "refresh_token": "loopback-refresh-two",
                    "expires_in": 3600,
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def begin(client, base):
    response = client.post(
        "/api/oauth/start",
        json={
            "authorize_url": base + "/authorize",
            "token_url": base + "/token",
            "client_id": "loopback-client",
            "scopes": ["read", "write"],
            "redirect_uri": "http://localhost/api/oauth/callback",
        },
    )
    assert response.status_code == 200
    with httpx.Client(trust_env=False) as browser:
        redirected = browser.get(response.json()["authorize_url"])
    assert redirected.status_code == 302
    return urlsplit(redirected.headers["location"])


def test_oauth_http_redirect_refresh_restart_and_logout(tmp_path, authorization_server):
    base, state = authorization_server
    (tmp_path / "workspace").mkdir()
    data = tmp_path / "data"
    with TestClient(create_app(data, tmp_path), raise_server_exceptions=False) as client:
        callback = begin(client, base)
        forged = client.get(callback.path + "?code=fake&state=forged")
        assert forged.status_code == 400 and not state["requests"]
        completed = client.get(callback.path + "?" + callback.query)
        assert completed.status_code == 200 and completed.headers["cache-control"] == "no-store"
        assert client.get(callback.path + "?" + callback.query).status_code == 400
        status = client.get("/api/oauth").json()
        assert status["authenticated"] and status["scopes"] == ["read", "write"]
        refreshed = client.post("/api/oauth/refresh")
        assert refreshed.status_code == 200 and "loopback-access" not in refreshed.text
        assert len(state["requests"]) == 2
    # Independent app/service instance reads persisted credentials and rotates the new token.
    with TestClient(create_app(data, tmp_path), raise_server_exceptions=False) as client:
        assert client.get("/api/oauth").json()["authenticated"]
        assert client.post("/api/oauth/refresh").status_code == 200
        assert state["requests"][-1]["refresh_token"] == ["loopback-refresh-two"]
        assert client.post("/api/oauth/logout").json()["authenticated"] is False
        assert client.post("/api/oauth/refresh").status_code == 400


def test_oauth_http_denial_and_refresh_failure_preserve_credentials(tmp_path, authorization_server):
    base, state = authorization_server
    (tmp_path / "workspace").mkdir()
    data = tmp_path / "data"
    with TestClient(create_app(data, tmp_path), raise_server_exceptions=False) as client:
        state["deny"] = True
        callback = begin(client, base)
        assert client.get(callback.path + "?" + callback.query).status_code == 400
        assert not state["requests"] and not client.get("/api/oauth").json()["authenticated"]
        state["deny"] = False
        callback = begin(client, base)
        assert client.get(callback.path + "?" + callback.query).status_code == 200
        before = (data / "credentials.json").read_bytes()
        state["refresh_status"] = 401
        result = client.post("/api/oauth/refresh")
        assert result.status_code == 400 and "loopback-refresh" not in result.text
        assert (data / "credentials.json").read_bytes() == before
