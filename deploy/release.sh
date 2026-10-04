#!/bin/sh
set -eu
ns=myclaude
previous=$(kubectl -n "$ns" get deployment myclaude -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
rollback() {
  trap - EXIT
  if [ -n "$previous" ]; then
    kubectl -n "$ns" set image deployment/myclaude app="$previous" workspace="$previous"
    kubectl -n "$ns" rollout status deployment/myclaude --timeout=180s || true
  fi
  printf 'FAILED commit=%s previous=%s\n' "$CI_COMMIT_SHA" "$previous" > deployment-result.txt
  exit 1
}
trap rollback EXIT
if [ -n "$previous" ]; then
  kubectl -n "$ns" exec deployment/myclaude -- python -c '
import datetime, pathlib, shutil, sqlite3
root = pathlib.Path("/data")
dest = root / "backups" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dest.mkdir(parents=True, exist_ok=False)
with sqlite3.connect(root / "app.sqlite") as source, sqlite3.connect(dest / "app.sqlite") as target:
    source.backup(target)
shutil.copy2(root / "settings.json", dest / "settings.json")
print("Pre-release SQLite and configuration backup complete")
'
fi
# Configuration and credentials are pre-provisioned, never managed by job artifacts.
sed "s|IMAGE_PLACEHOLDER|$IMAGE_REPOSITORY:$CI_COMMIT_SHA|g" deploy/kubernetes.yaml | kubectl apply -f -
kubectl -n "$ns" rollout status deployment/myclaude --timeout=300s
kubectl -n "$ns" exec deployment/myclaude -- python -c '
import json, os, re, urllib.request
base = "http://127.0.0.1:8000"
health = json.load(urllib.request.urlopen(base + "/api/health", timeout=10))
assert health["ok"]
html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
assets = re.findall(r"(?:src|href)=\"([^\"]*/assets/[^\"]+)\"", html)
assert len(assets) >= 2, "Frontend assets missing from index"
prefix = os.environ.get("MYCLAUDE_ROOT_PATH", "").rstrip("/")
for asset in assets:
    response = urllib.request.urlopen(base + asset.removeprefix(prefix), timeout=10)
    assert response.status == 200 and "text/html" not in response.headers.get("Content-Type", "")
print({**health, "frontend_assets": len(assets)})
'
printf 'SUCCESS commit=%s image=%s:%s\n' "$CI_COMMIT_SHA" "$IMAGE_REPOSITORY" "$CI_COMMIT_SHA" > deployment-result.txt
trap - EXIT
