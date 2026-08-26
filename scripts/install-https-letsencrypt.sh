#!/usr/bin/env bash
# Issue / renew Let's Encrypt certs and enable HTTPS nginx.
#
# Prerequisites:
#   - DNS A/AAAA for each domain you want on the cert → this instance
#   - Security group allows TCP 80 and TCP 443
#   - Dashboard container healthy on :8081
#
# Usage (on the EC2 host):
#   ./scripts/install-https-letsencrypt.sh
#   CERTBOT_EMAIL=you@example.com ./scripts/install-https-letsencrypt.sh
#   CERTBOT_DOMAINS="eeesoc.com" ./scripts/install-https-letsencrypt.sh   # apex only
#
# Domains that do not resolve (NXDOMAIN) are skipped automatically so a
# missing www record does not block HTTPS for the apex.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Default candidate list; override with CERTBOT_DOMAINS="eeesoc.com www.eeesoc.com"
if [ -n "${CERTBOT_DOMAINS:-}" ]; then
  # shellcheck disable=SC2206
  CANDIDATES=(${CERTBOT_DOMAINS})
else
  CANDIDATES=(eeesoc.com www.eeesoc.com)
fi
EMAIL="${CERTBOT_EMAIL:-}"
WEBROOT="/var/www/certbot"
BOOTSTRAP_CONF="$ROOT/scripts/nginx-eeesoc-http-bootstrap.conf"
HTTPS_TEMPLATE="$ROOT/scripts/nginx-eeesoc-dashboard.conf"
NGINX_CONF="/etc/nginx/conf.d/eeesoc-dashboard.conf"

domain_resolves() {
  local host="$1"
  getent ahostsv4 "$host" >/dev/null 2>&1 && return 0
  getent ahostsv6 "$host" >/dev/null 2>&1 && return 0
  # Fallback when getent ahosts is unavailable
  python3 - "$host" <<'PY' 2>/dev/null
import socket, sys
host = sys.argv[1]
try:
    socket.getaddrinfo(host, 80)
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx not installed — run ./scripts/install-nginx-amazon-linux.sh first"
  exit 1
fi

# Install certbot (Amazon Linux 2023 / 2)
if ! command -v certbot >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y certbot
  elif command -v yum >/dev/null 2>&1; then
    yum install -y certbot || yum install -y certbot python3-certbot
  else
    echo "Install certbot manually, then re-run this script."
    exit 1
  fi
fi

DOMAINS=()
SKIPPED=()
for d in "${CANDIDATES[@]}"; do
  if domain_resolves "$d"; then
    DOMAINS+=("$d")
    echo "DNS OK  $d"
  else
    SKIPPED+=("$d")
    echo "DNS SKIP $d (no A/AAAA — NXDOMAIN or not propagated)"
  fi
done

if [ "${#DOMAINS[@]}" -eq 0 ]; then
  echo "ERROR: none of the candidate domains resolve: ${CANDIDATES[*]}"
  echo "Add an A record for eeesoc.com (and optionally www) pointing at this instance, wait for DNS, retry."
  exit 1
fi

if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo "Continuing without: ${SKIPPED[*]}"
  echo "Add those DNS records later, then re-run this script to expand the cert."
fi

PRIMARY="${DOMAINS[0]}"
SERVER_NAME_LINE="${DOMAINS[*]}"

mkdir -p "$WEBROOT"
# Bootstrap HTTP (ACME + proxy) so we can obtain certs before enabling :443.
# Patch server_name to the domains we will actually request.
sed "s/^[[:space:]]*server_name .*/    server_name ${SERVER_NAME_LINE};/" \
  "$BOOTSTRAP_CONF" >"$NGINX_CONF"
chmod 0644 "$NGINX_CONF"
rm -f /etc/nginx/conf.d/default.conf
nginx -t
systemctl enable --now nginx
systemctl reload nginx

EMAIL_ARGS=()
if [ -n "$EMAIL" ]; then
  EMAIL_ARGS=(--email "$EMAIL")
else
  EMAIL_ARGS=(--register-unsafely-without-email)
  echo "NOTE: set CERTBOT_EMAIL=you@example.com for expiry notices."
fi

DOMAIN_ARGS=()
for d in "${DOMAINS[@]}"; do
  DOMAIN_ARGS+=(-d "$d")
done

echo "Requesting Let's Encrypt certificate for: ${DOMAINS[*]}"
certbot certonly \
  --webroot -w "$WEBROOT" \
  "${DOMAIN_ARGS[@]}" \
  "${EMAIL_ARGS[@]}" \
  --agree-tos \
  --non-interactive \
  --keep-until-expiring \
  --cert-name "$PRIMARY"

# Deploy HTTPS nginx config with server_name matching the issued cert.
sed "s/^[[:space:]]*server_name .*/    server_name ${SERVER_NAME_LINE};/" \
  "$HTTPS_TEMPLATE" >"$NGINX_CONF"
chmod 0644 "$NGINX_CONF"
nginx -t
systemctl reload nginx

# Reload nginx after every successful renew.
install -d /etc/letsencrypt/renewal-hooks/deploy
cat >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env bash
systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Ensure renew timer is present (Amazon Linux packages leave it disabled).
systemctl enable --now certbot-renew.timer 2>/dev/null || true
systemctl enable --now certbot.timer 2>/dev/null || true
systemctl start certbot-renew.timer 2>/dev/null || true

echo
echo "HTTPS enabled for: ${DOMAINS[*]}"
for d in "${DOMAINS[@]}"; do
  echo "  https://${d}/"
done
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo "Still missing DNS (not on cert yet): ${SKIPPED[*]}"
fi
echo "Open security group inbound TCP 443 if you have not already."
curl -fsSk "https://127.0.0.1/health" -H "Host: ${PRIMARY}" \
  || curl -fsS "https://${PRIMARY}/health" \
  || echo "(HTTPS health check failed — check DNS / SG 443 / cert paths)"
