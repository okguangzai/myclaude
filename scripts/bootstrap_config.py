"""First run only: reuse explicitly supplied local credentials without printing them."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.config import ConfigStore

store = ConfigStore()
if "--import-claw-rag" in sys.argv:
    source = root.parent / "claude" / ".claw-local" / "rag.local.json"
    if not source.exists():
        raise SystemExit("Previous RAG configuration not found; configure in the UI.")
    previous = json.loads(source.read_text(encoding="utf-8-sig"))
    store.update(
        {
            "embedding_model": previous["embedding_model"],
            "embedding_base_url": previous["embedding_base_url"],
            "embedding_api_key": previous["embedding_api_key"],
            "qdrant_api_key": previous["qdrant_api_key"],
            "qdrant_url": "http://127.0.0.1:17333",
            "qdrant_collection": "myclaude_learning_v1",
            "ssh_enabled": True,
            "ssh_host": previous["ssh_host"],
            "ssh_port": previous["ssh_port"],
            "ssh_remote_host": previous["qdrant_remote_host"],
        }
    )
print("Local configuration ready. Credentials are not displayed.")
