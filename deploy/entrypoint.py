"""Initialize persistent configuration once, then run the container service."""

import os
import shutil
from pathlib import Path

import uvicorn

data = Path(os.environ.get("MYCLAUDE_DATA_DIR", "/data"))
data.mkdir(parents=True, exist_ok=True)
seed = Path("/run/config/settings.json")
target = data / "settings.json"
if not target.exists() and seed.exists():
    shutil.copyfile(seed, target)
    target.chmod(0o600)

uvicorn.run(
    "app.main:app",
    host="0.0.0.0",
    port=8000,
    access_log=False,
    proxy_headers=False,
    timeout_graceful_shutdown=30,
)
