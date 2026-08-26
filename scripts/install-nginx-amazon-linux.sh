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

# Prefer full HTTPS conf when certs already exist; otherwise HTTP bootstrap
# (ACME-ready). Full HTTPS is enabled by ./scripts/install-https-letsencrypt.sh.
BOOTSTRAP="$ROOT/scripts/nginx-eeesoc-http-bootstrap.conf"
HTTPS_CONF="$ROOT/scripts/nginx-eeesoc-dashboard.conf"
if [ -f /etc/letsencrypt/live/eeesoc.com/fullchain.pem ] && [ -f "$HTTPS_CONF" ]; then
  CONF_SRC="$HTTPS_CONF"
  echo "Found existing Let's Encrypt cert — installing HTTPS nginx config."
elif [ -f "$BOOTSTRAP" ]; then
  CONF_SRC="$BOOTSTRAP"
fi

# Stock package configs often also claim :80.
rm -f /etc/nginx/conf.d/default.conf
# Named hosts (eeesoc.com / www) can coexist with julia's default_server
# as long as Host headers match. Still warn if both are present.
if [ -f /etc/nginx/conf.d/julia-dashboard.conf ]; then
  echo "NOTE: julia-dashboard.conf is also installed."
  echo "eeesoc answers for eeesoc.com / www.eeesoc.com; julia keeps default_server."
fi

install -m 0644 "$CONF_SRC" /etc/nginx/conf.d/eeesoc-dashboard.conf

nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo
echo "nginx HTTP ready for eeesoc.com / www.eeesoc.com (:80 → :8081)"
echo "Point DNS A records at this instance (SG: TCP 80 + 443)."
echo "Then enable HTTPS:  ./scripts/install-https-letsencrypt.sh"
curl -fsS -H 'Host: eeesoc.com' http://127.0.0.1/health \
  || echo "(local /health via nginx failed — is eeesoc-dashboard up?)"
