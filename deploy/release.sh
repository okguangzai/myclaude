#!/bin/sh
set -eu
ns=myclaude
previous=$(kubectl -n "$ns" get deployment myclaude -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
rollback() {
  trap - EXIT
  if [ -n "$previous" ]; then
    kubectl -n "$ns" set image deployment/myclaude app="$previous"
    kubectl -n "$ns" rollout status deployment/myclaude --timeout=180s || true
  fi
  printf 'FAILED commit=%s previous=%s\n' "$CI_COMMIT_SHA" "$previous" > deployment-result.txt
  exit 1
}
trap rollback EXIT
# Configuration and credentials are pre-provisioned, never managed by job artifacts.
sed "s|IMAGE_PLACEHOLDER|$IMAGE_REPOSITORY:$CI_COMMIT_SHA|g" deploy/kubernetes.yaml | kubectl apply -f -
kubectl -n "$ns" rollout status deployment/myclaude --timeout=300s
kubectl -n "$ns" exec deployment/myclaude -- python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/health")); assert r["ok"]; print(r)'
printf 'SUCCESS commit=%s image=%s:%s\n' "$CI_COMMIT_SHA" "$IMAGE_REPOSITORY" "$CI_COMMIT_SHA" > deployment-result.txt
trap - EXIT
