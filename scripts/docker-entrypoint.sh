#!/usr/bin/env bash
# Container entrypoint: serve immediately when cache exists.
# A blocking --warm on every restart takes the site down (nginx 502) if
# football-data.co.uk is slow or errors — deploy.sh then health-checks a
# box that never bound :8081.
set -euo pipefail

SEASON="${EEESOC_SEASON:-EPL:2025}"
PORT="${EEESOC_PORT:-8081}"
HOST="${EEESOC_HOST:-0.0.0.0}"
CACHE_DIR="${EEESOC_CACHE:-/data/cache}"

export EEESOC_CACHE="$CACHE_DIR"
mkdir -p "$EEESOC_CACHE/seasons"

echo "[eeesoc] cache=${EEESOC_CACHE} season=${SEASON} bind=${HOST}:${PORT}"
echo "[eeesoc] git=${EEESOC_GIT_SHA:-unknown} @ ${EEESOC_GIT_COMMIT_TIME:-unknown}"

season_file="${EEESOC_CACHE}/seasons/${SEASON//:/_}.json"
if [[ -s "$season_file" ]]; then
  echo "[eeesoc] cache hit $(basename "$season_file") — bind now, refresh in background"
  (
    uv run eeesoc --warm "$SEASON" && echo "[eeesoc] warm done"
  ) || echo "[eeesoc] warm failed; keeping existing cache" &
else
  echo "[eeesoc] empty cache — warming before bind"
  uv run eeesoc --warm "$SEASON" || echo "[eeesoc] warm failed; starting with empty corpus"
fi

exec uv run eeesoc --dashboard --host "$HOST" --port "$PORT" --season "$SEASON"
