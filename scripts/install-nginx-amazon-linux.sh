#!/usr/bin/env bash
# Install nginx on Amazon Linux and point :80 → eeesoc :8081 (julia-style).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/scripts/nginx-eeesoc-dashboard.conf"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

if ! command -v nginx >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nginx
  else
    echo "Neither dnf nor yum found — install nginx manually."
    exit 1
  fi
fi

# Stock package configs often also claim :80.
rm -f /etc/nginx/conf.d/default.conf
# If julia's config is on this same host it will conflict as another
# default_server on :80 — warn loudly.
if [ -f /etc/nginx/conf.d/julia-dashboard.conf ]; then
  echo "WARNING: /etc/nginx/conf.d/julia-dashboard.conf already exists."
  echo "Both cannot be default_server on :80. Move julia aside or use a path split."
fi

install -m 0644 "$CONF_SRC" /etc/nginx/conf.d/eeesoc-dashboard.conf

nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo
echo "nginx is proxying :80 → 127.0.0.1:8081"
echo "Open http://<EC2-public-IP>/  (security group must allow TCP 80)"
curl -fsS http://127.0.0.1/health || echo "(local /health via nginx failed — is eeesoc-dashboard up?)"
