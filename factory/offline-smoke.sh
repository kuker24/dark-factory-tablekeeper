#!/usr/bin/env bash
# Clean-container, NO outbound network smoke test for one stage folder.
# Usage: scripts/offline-smoke.sh /abs/path/to/result/stage-1 [port]
# Ini hanya smoke test. Tes resmi = `python -m harness run ... --mode isolated`.
set -euo pipefail
DIR="${1:?usage: $0 <stage-folder> [port]}"; PORT="${2:-8080}"
TAG="df-smoke-$(basename "$DIR")"; NAME="${TAG}-run"; NET="df-smoke-internal"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== build (network allowed during build) =="
docker build --no-cache -t "$TAG" "$DIR"

echo "== test A: --network none (container has no network at all) =="
docker run -d --name "$NAME" --network none --cpus 2 --memory 2g -e PORT="$PORT" "$TAG" >/dev/null
sleep 3
docker logs "$NAME" | tail -n 20 || true
if docker ps --filter "name=$NAME" --filter status=running -q | grep -q .; then
  echo "OK: container still running with --network none"
else
  echo "FAIL: container exited with --network none"; exit 1
fi
# health check from INSIDE the container (uses python/wget/curl from the image)
docker exec "$NAME" sh -c "python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$PORT/health').read().decode())\" 2>/dev/null \
  || wget -qO- http://127.0.0.1:$PORT/health 2>/dev/null || curl -fsS http://127.0.0.1:$PORT/health" \
  && echo "OK: /health answered inside --network none" \
  || echo "WARN: no python/wget/curl in image to probe from inside; use test B"
docker rm -f "$NAME" >/dev/null

echo "== test B: internal network (no outbound), probed from a sibling container =="
docker pull -q curlimages/curl >/dev/null
docker network create --internal "$NET" >/dev/null
docker run -d --name "$NAME" --network "$NET" --cpus 2 --memory 2g -e PORT="$PORT" "$TAG" >/dev/null
for i in $(seq 1 60); do
  if docker run --rm --network "$NET" curlimages/curl -fsS "http://$NAME:$PORT/health" 2>/dev/null; then
    echo; echo "OK: healthy after ~${i}s on an internal (no-outbound) network"; exit 0
  fi
  sleep 1
done
echo "FAIL: no healthy /health within 60s"; docker logs "$NAME" | tail -n 50; exit 1
