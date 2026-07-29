#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

staging_dir="${1:-/tmp/ceavpn-worker}"
secret_file="${2:-/tmp/ceavpn-worker-secrets.env}"
epoch_mode="${3:-preserve}"
admin_file="/root/ceavpn-admin.env"
node_file="/root/ceavpn-node.env"
worker_env_file="/etc/ceavpn/worker.env"

if [[ "$epoch_mode" != "preserve" && "$epoch_mode" != "--rotate-epoch" ]]; then
  echo "Third argument must be --rotate-epoch when explicitly rebuilding a node." >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "Missing required command: openssl" >&2
  exit 1
fi

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

node_mode="direct"
if [[ -s "$node_file" ]]; then
  # shellcheck disable=SC1090
  source "$node_file"
  node_mode="${CEAVPN_NODE_MODE:-direct}"
fi
case "$node_mode" in
  whitelist)
    default_inbound_tags="VLESS XHTTP REALITY"
    default_vless_flow=""
    ;;
  direct|lte)
    default_inbound_tags="VLESS TCP REALITY,VLESS WS TLS FALLBACK"
    default_vless_flow="xtls-rprx-vision"
    ;;
  *)
    echo "Unsupported CEAVPN_NODE_MODE." >&2
    exit 1
    ;;
esac
worker_inbound_tags="${MARZBAN_INBOUND_TAGS:-$default_inbound_tags}"
if [[ ${MARZBAN_VLESS_FLOW+x} ]]; then
  worker_vless_flow="$MARZBAN_VLESS_FLOW"
else
  worker_vless_flow="$default_vless_flow"
fi

: "${VPN_WORKER_SECRET:?VPN_WORKER_SECRET is required}"
: "${MARZBAN_BOT_USERNAME:?MARZBAN_BOT_USERNAME is required}"
: "${MARZBAN_BOT_PASSWORD:?MARZBAN_BOT_PASSWORD is required}"

worker_id="${VPN_WORKER_ID:-cea-vpn-nl1}"
railway_base_url="${VPN_RAILWAY_BASE_URL:-https://railvay-production-8ba7.up.railway.app}"
subscription_base_url="${VPN_SUBSCRIPTION_BASE_URL:-https://sub.79-137-197-51.sslip.io:8443}"

existing_worker_epoch=""
if [[ -e "$worker_env_file" ]]; then
  if [[ ! -f "$worker_env_file" || -L "$worker_env_file" ]]; then
    echo "Existing worker environment is not a regular file." >&2
    exit 1
  fi
  existing_worker_epoch="$(
    sed -n 's/^VPN_WORKER_EPOCH=//p' "$worker_env_file"
  )"
  if grep -q '^VPN_WORKER_EPOCH=' "$worker_env_file" &&
    [[ ! "$existing_worker_epoch" =~ ^e[0-9a-f]{32}$ ]]; then
    echo "Existing VPN worker epoch is invalid." >&2
    exit 1
  fi
fi
supplied_worker_epoch="${VPN_WORKER_EPOCH:-}"
if [[ -n "$supplied_worker_epoch" &&
  ! "$supplied_worker_epoch" =~ ^e[0-9a-f]{32}$ ]]; then
  echo "Supplied VPN worker epoch is invalid." >&2
  exit 1
fi
if [[ "$epoch_mode" == "--rotate-epoch" ]]; then
  worker_epoch="e$(openssl rand -hex 16)"
elif [[ -n "$existing_worker_epoch" ]]; then
  worker_epoch="$existing_worker_epoch"
elif [[ -n "$supplied_worker_epoch" ]]; then
  worker_epoch="$supplied_worker_epoch"
else
  worker_epoch="e$(openssl rand -hex 16)"
fi
if [[ ! "$worker_epoch" =~ ^e[0-9a-f]{32}$ ]]; then
  echo "Could not establish a valid VPN worker epoch." >&2
  exit 1
fi

install -d -o root -g root -m 0755 /opt/ceavpn /etc/ceavpn
install -o root -g root -m 0755 "$staging_dir/worker.py" /opt/ceavpn/worker.py
install -o root -g root -m 0644 \
  "$staging_dir/ceavpn-worker.service" \
  /etc/systemd/system/ceavpn-worker.service

umask 077
{
  printf 'VPN_WORKER_ID=%s\n' "$worker_id"
  printf 'VPN_WORKER_SECRET=%s\n' "$VPN_WORKER_SECRET"
  printf 'VPN_WORKER_EPOCH=%s\n' "$worker_epoch"
  printf 'VPN_RAILWAY_BASE_URL=%s\n' \
    "$railway_base_url"
  printf 'VPN_SUBSCRIPTION_BASE_URL=%s\n' \
    "$subscription_base_url"
  printf 'MARZBAN_BASE_URL=%s\n' 'http://127.0.0.1:8000'
  printf 'MARZBAN_BOT_USERNAME=%s\n' "$MARZBAN_BOT_USERNAME"
  printf 'MARZBAN_BOT_PASSWORD=%s\n' "$MARZBAN_BOT_PASSWORD"
  printf 'MARZBAN_INBOUND_TAGS=%s\n' "$worker_inbound_tags"
  printf 'MARZBAN_VLESS_FLOW=%s\n' "$worker_vless_flow"
  printf 'VPN_WORKER_POLL_INTERVAL_SECONDS=%s\n' '3'
  printf 'VPN_WORKER_HTTP_TIMEOUT_SECONDS=%s\n' '15'
  printf 'VPN_WORKER_LEASE_SECONDS=%s\n' '120'
  printf 'VPN_WORKER_LOG_LEVEL=%s\n' 'INFO'
} > "$worker_env_file"
chmod 0600 "$worker_env_file"

systemctl daemon-reload
systemctl enable ceavpn-worker.service
systemctl restart ceavpn-worker.service

rm -f -- "$secret_file"
echo "CEA VPN worker installed."
