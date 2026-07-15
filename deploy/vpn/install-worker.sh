#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

staging_dir="${1:-/tmp/ceavpn-worker}"
secret_file="${2:-/tmp/ceavpn-worker-secrets.env}"
admin_file="/root/ceavpn-admin.env"

for required_file in \
  "$staging_dir/worker.py" \
  "$staging_dir/ceavpn-worker.service" \
  "$secret_file" \
  "$admin_file"; do
  if [[ ! -f "$required_file" ]]; then
    echo "Missing required file: $required_file" >&2
    exit 1
  fi
done

# These two files are root-only and their values are never printed.
set -a
source "$admin_file"
source "$secret_file"
set +a

: "${VPN_WORKER_SECRET:?VPN_WORKER_SECRET is required}"
: "${MARZBAN_BOT_USERNAME:?MARZBAN_BOT_USERNAME is required}"
: "${MARZBAN_BOT_PASSWORD:?MARZBAN_BOT_PASSWORD is required}"

install -d -o root -g root -m 0755 /opt/ceavpn /etc/ceavpn
install -o root -g root -m 0755 "$staging_dir/worker.py" /opt/ceavpn/worker.py
install -o root -g root -m 0644 \
  "$staging_dir/ceavpn-worker.service" \
  /etc/systemd/system/ceavpn-worker.service

umask 077
{
  printf 'VPN_WORKER_ID=%s\n' 'cea-vpn-nl1'
  printf 'VPN_WORKER_SECRET=%s\n' "$VPN_WORKER_SECRET"
  printf 'VPN_RAILWAY_BASE_URL=%s\n' \
    'https://railvay-production-8ba7.up.railway.app'
  printf 'VPN_SUBSCRIPTION_BASE_URL=%s\n' \
    'https://sub.79-137-197-51.sslip.io:8443'
  printf 'MARZBAN_BASE_URL=%s\n' 'http://127.0.0.1:8000'
  printf 'MARZBAN_BOT_USERNAME=%s\n' "$MARZBAN_BOT_USERNAME"
  printf 'MARZBAN_BOT_PASSWORD=%s\n' "$MARZBAN_BOT_PASSWORD"
  printf 'MARZBAN_INBOUND_TAG=%s\n' 'VLESS TCP REALITY'
  printf 'VPN_WORKER_POLL_INTERVAL_SECONDS=%s\n' '3'
  printf 'VPN_WORKER_HTTP_TIMEOUT_SECONDS=%s\n' '15'
  printf 'VPN_WORKER_LEASE_SECONDS=%s\n' '120'
  printf 'VPN_WORKER_LOG_LEVEL=%s\n' 'INFO'
} > /etc/ceavpn/worker.env
chmod 0600 /etc/ceavpn/worker.env

systemctl daemon-reload
systemctl enable ceavpn-worker.service
systemctl restart ceavpn-worker.service

rm -f -- "$secret_file"
echo "CEA VPN worker installed."
