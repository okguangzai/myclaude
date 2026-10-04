"""Exercise real SSH Git plugin install/update; remove only the generated fixture repo."""

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import ConfigStore  # noqa: E402
from app.db import Database  # noqa: E402
from app.extensions import Extensions  # noqa: E402


async def verify(host, port):
    ssh = ["ssh", "-o", "BatchMode=yes", "-p", str(port), host]
    created = subprocess.check_output(
        [*ssh, "mktemp -d /home/csg/work/myclaude-plugin-verify-XXXXXXXX"], text=True
    ).strip()
    # Only a directory returned by our mktemp, constrained to the explicit fixture prefix.
    if not created.startswith(
        "/home/csg/work/myclaude-plugin-verify-"
    ) or "/" in created.removeprefix("/home/csg/work/"):
        raise ValueError("Unexpected temporary repository path")
    remote = f"ssh://{host}:{port}{created}/fixture.git"
    try:
        subprocess.run(
            [*ssh, "git init --bare " + shlex.quote(created + "/fixture.git")],
            check=True,
            capture_output=True,
        )
        with tempfile.TemporaryDirectory(prefix="myclaude-plugin-client-") as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            repo = root / "source"
            (repo / ".claude-plugin").mkdir(parents=True)

            def git(*args):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

            git("init")
            git("config", "user.name", "MyClaude acceptance")
            git("config", "user.email", "acceptance@localhost")
            git("remote", "add", "origin", remote)
            manifest = repo / ".claude-plugin/plugin.json"

            def publish(version):
                manifest.write_text(
                    json.dumps({"name": "remote-fixture", "version": version, "tools": []}),
                    encoding="utf-8",
                )
                git("add", ".")
                git("commit", "-m", "fixture " + version)
                git("push", "origin", "HEAD:refs/heads/master")

            publish("1.0.0")
            store = ConfigStore(root / "data", root)
            async with httpx.AsyncClient() as client:
                ext = Extensions(Database(store.data / "app.sqlite"), store, client)
                try:
                    item = await ext.plugins.install_source(remote)
                    assert item["manifest"]["version"] == "1.0.0"
                    publish("2.0.0")
                    updated = await ext.plugins.update("remote-fixture")
                    assert updated["new_version"] == "2.0.0"
                    await ext.plugins.uninstall("remote-fixture")
                finally:
                    await ext.close()
            report = {"transport": "ssh", "install": "1.0.0", "update": "2.0.0", "uninstall": True}
            (ROOT / ".local/remote-plugin-verification.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            print(json.dumps(report))
    finally:
        code = (
            "import pathlib,shutil; p=pathlib.Path("
            + repr(created)
            + "); assert p.parent == pathlib.Path('/home/csg/work') and p.name.startswith('myclaude-plugin-verify-'); shutil.rmtree(p)"
        )
        subprocess.run([*ssh, "python3 -c " + shlex.quote(code)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="csg@101.96.204.240")
    parser.add_argument("--port", type=int, default=16022)
    args = parser.parse_args()
    asyncio.run(verify(args.host, args.port))
