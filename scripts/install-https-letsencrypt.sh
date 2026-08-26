#!/usr/bin/env bash
# Issue / renew Let's Encrypt certs for eeesoc.com + www and enable HTTPS nginx.
#
# Prerequisites:
#   - DNS A records for eeesoc.com and www.eeesoc.com → this instance
#   - Security group allows TCP 80 and TCP 443
#   - Dashboard container healthy on :8081
#
# Usage (on the EC2 host):
#   ./scripts/install-https-letsencrypt.sh
#   CERTBOT_EMAIL=you@example.com ./scripts/install-https-letsencrypt.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAINS=(eeesoc.com www.eeesoc.com)
PRIMARY="${DOMAINS[0]}"
EMAIL="${CERTBOT_EMAIL:-}"
WEBROOT="/var/www/certbot"
BOOTSTRAP_CONF="$ROOT/scripts/nginx-eeesoc-http-bootstrap.conf"
HTTPS_CONF="$ROOT/scripts/nginx-eeesoc-dashboard.conf"
NGINX_CONF="/etc/nginx/conf.d/eeesoc-dashboard.conf"

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

mkdir -p "$WEBROOT"
# Bootstrap HTTP (ACME + proxy) so we can obtain certs before enabling :443.
install -m 0644 "$BOOTSTRAP_CONF" "$NGINX_CONF"
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

# Deploy HTTPS-capable nginx config (redirect :80 → :443 + SSL proxy).
install -m 0644 "$HTTPS_CONF" "$NGINX_CONF"
nginx -t
systemctl reload nginx

# Reload nginx after every successful renew.
install -d /etc/letsencrypt/renewal-hooks/deploy
cat >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env bash
systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Ensure renew timer/cron is present (package usually installs it).
systemctl enable --now certbot-renew.timer 2>/dev/null || true
systemctl enable --now certbot.timer 2>/dev/null || true

echo
echo "HTTPS enabled for https://eeesoc.com/ and https://www.eeesoc.com/"
echo "Open security group inbound TCP 443 if you have not already."
curl -fsSk "https://127.0.0.1/health" -H "Host: eeesoc.com" \
  || curl -fsS "https://eeesoc.com/health" \
  || echo "(HTTPS health check failed — check DNS / SG 443 / cert paths)"
