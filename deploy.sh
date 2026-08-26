#!/usr/bin/env bash
#
# Pull latest code and rebuild the dashboard container.
#
# Usage (on the EC2 host, from the repo root):
#   ./deploy.sh
#
# Called automatically by .github/workflows/deploy-ec2.yml on pushes to
# main (via SSH). Safe to run by hand any time.
#
# Mirrors tekneeq/julia's deploy.sh (without julia's scheduler/poller steps).

set -euo pipefail

cd "$(dirname "$0")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

log "=== deploy start (cwd=$(pwd), rev=$(git rev-parse --short HEAD 2>/dev/null || echo '?')) ==="

log "1/2  rebuild dashboard container (git pull + docker build/run)"
./restart.sh

sleep 3
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx eeesoc-dashboard \
   && ! sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -qx eeesoc-dashboard; then
    log "ERROR: eeesoc-dashboard container is not running after restart.sh"
    exit 1
fi

log "2/2  health check"
# Give warm+bind a few seconds on cold cache
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:8081/health" >/dev/null 2>&1; then
        log "healthy: $(curl -fsS http://127.0.0.1:8081/health | tr -d '\n')"
        break
    fi
    if [ "$i" -eq 10 ]; then
        log "ERROR: /health did not respond after restart"
        docker logs --tail 80 eeesoc-dashboard 2>/dev/null \
          || sudo docker logs --tail 80 eeesoc-dashboard 2>/dev/null \
          || true
        exit 1
    fi
    sleep 3
done

log "=== deploy done (rev=$(git rev-parse --short HEAD)) ==="
docker ps --filter name=eeesoc-dashboard --format '{{.Names}} {{.Status}} {{.Image}}' 2>/dev/null \
  || sudo docker ps --filter name=eeesoc-dashboard --format '{{.Names}} {{.Status}} {{.Image}}'
