#!/usr/bin/env bash
# Container entrypoint: warm EPL cache (cheap when already cached), then serve.
set -euo pipefail

SEASON="${EEESOC_SEASON:-EPL:2025}"
PORT="${EEESOC_PORT:-8081}"
HOST="${EEESOC_HOST:-0.0.0.0}"
CACHE_DIR="${EEESOC_CACHE:-/data/cache}"

export EEESOC_CACHE="$CACHE_DIR"
mkdir -p "$EEESOC_CACHE"

echo "[eeesoc] cache=${EEESOC_CACHE} season=${SEASON} bind=${HOST}:${PORT}"
echo "[eeesoc] git=${EEESOC_GIT_SHA:-unknown} @ ${EEESOC_GIT_COMMIT_TIME:-unknown}"

uv run eeesoc --warm "$SEASON"
exec uv run eeesoc --dashboard --host "$HOST" --port "$PORT" --season "$SEASON"
