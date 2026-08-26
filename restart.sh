#!/usr/bin/env bash
# Pull latest main, rebuild, and recreate the eeesoc-dashboard container.
# Same shape as tekneeq/julia's restart.sh.
set -euo pipefail
cd "$(dirname "$0")"

require_docker() {
  if command -v docker >/dev/null 2>&1; then
    return 0
  fi
  # Common on fresh AL2/AL2023 before usermod + re-login.
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    echo "NOTE: using sudo docker (add yourself to the docker group to skip sudo)."
    DOCKER=(sudo docker)
    return 0
  fi
  cat >&2 <<'EOF'
ERROR: docker is not installed (or not on PATH).

Install once on this Amazon Linux box:
  ./scripts/install-docker-amazon-linux.sh
  # then re-login (or: newgrp docker)
  ./deploy.sh

If julia already runs Docker on another instance, clone eeesoc there instead
(or install Docker here the same way that box was set up).
EOF
  exit 127
}

DOCKER=(docker)
require_docker

git pull

GIT_SHA="$(git rev-parse --short HEAD)"
GIT_COMMIT_TIME="$(git show -s --format=%cI HEAD)"

mkdir -p data/cache

"${DOCKER[@]}" build \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --build-arg "GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    -t eeesoc-dashboard:latest .
"${DOCKER[@]}" rm -f eeesoc-dashboard 2>/dev/null || true
"${DOCKER[@]}" run -d --name eeesoc-dashboard --restart unless-stopped \
    -p 8081:8081 \
    -v "$(pwd)/data/cache:/data/cache" \
    -e "EEESOC_GIT_SHA=${GIT_SHA}" \
    -e "EEESOC_GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    -e "EEESOC_SEASON=${EEESOC_SEASON:-EPL:2025}" \
    -e "EEESOC_CACHE=/data/cache" \
    eeesoc-dashboard:latest

echo "Started eeesoc-dashboard at ${GIT_SHA} (${GIT_COMMIT_TIME})"
