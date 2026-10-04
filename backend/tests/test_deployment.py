from app.main import create_app
from fastapi.testclient import TestClient


def test_reverse_proxy_prefix_hosts_and_origin(tmp_path, monkeypatch):
    (tmp_path / "workspace").mkdir()
    assets = tmp_path / "frontend/dist/assets"
    assets.mkdir(parents=True)
    (assets / "probe.js").write_text("window.deploymentReady = true;")
    monkeypatch.setenv("MYCLAUDE_ROOT_PATH", "/myclaude/")
    monkeypatch.setenv("MYCLAUDE_ALLOWED_HOSTS", "gitlab.csghome.fun")
    monkeypatch.setenv("MYCLAUDE_ALLOWED_ORIGINS", "https://gitlab.csghome.fun")
    app = create_app(tmp_path / "data", tmp_path)
    with TestClient(app, base_url="https://gitlab.csghome.fun") as client:
        assert client.get("/myclaude/api/health").json()["ok"]
        # Uvicorn root_path must preserve the prefix in scope.path after ingress stripping.
        asset = client.get("/myclaude/assets/probe.js")
        assert asset.status_code == 200
        assert asset.text == "window.deploymentReady = true;"
        schema = client.get("/myclaude/api/openapi.json").json()
        assert schema["servers"][0]["url"] == "/myclaude"
        assert client.get("/api/health", headers={"host": "attacker.invalid"}).status_code == 400
        assert (
            client.post("/api/sessions", headers={"origin": "https://attacker.invalid"}).status_code
            == 403
        )
        assert (
            client.post(
                "/myclaude/api/sessions", headers={"origin": "https://attacker.invalid"}
            ).status_code
            == 403
        )
        response = client.get("/api/health", headers={"origin": "https://gitlab.csghome.fun"})
        assert response.status_code == 200


def test_local_defaults_do_not_allow_public_hosts(tmp_path, monkeypatch):
    (tmp_path / "workspace").mkdir()
    for name in ("MYCLAUDE_ROOT_PATH", "MYCLAUDE_ALLOWED_HOSTS", "MYCLAUDE_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(tmp_path / "data", tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health", headers={"host": "gitlab.csghome.fun"}).status_code == 400
