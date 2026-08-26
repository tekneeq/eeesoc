#!/usr/bin/env bash
# Pull latest main, rebuild, and recreate the eeesoc-dashboard container.
# Same shape as tekneeq/julia's restart.sh.
set -euo pipefail
cd "$(dirname "$0")"

git pull

GIT_SHA="$(git rev-parse --short HEAD)"
GIT_COMMIT_TIME="$(git show -s --format=%cI HEAD)"

mkdir -p data/cache

docker build \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --build-arg "GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    -t eeesoc-dashboard:latest .
docker rm -f eeesoc-dashboard 2>/dev/null || true
docker run -d --name eeesoc-dashboard --restart unless-stopped \
    -p 8081:8081 \
    -v "$(pwd)/data/cache:/data/cache" \
    -e "EEESOC_GIT_SHA=${GIT_SHA}" \
    -e "EEESOC_GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    -e "EEESOC_SEASON=${EEESOC_SEASON:-EPL:2025}" \
    -e "EEESOC_CACHE=/data/cache" \
    eeesoc-dashboard:latest

echo "Started eeesoc-dashboard at ${GIT_SHA} (${GIT_COMMIT_TIME})"
