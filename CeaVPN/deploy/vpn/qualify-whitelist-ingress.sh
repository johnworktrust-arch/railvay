#!/usr/bin/env bash
set -Eeuo pipefail

node_file="${CEAVPN_NODE_FILE:-/root/ceavpn-node.env}"
qualification_file="${CEAVPN_WHITELIST_QUALIFICATION_FILE:-/root/ceavpn-whitelist-qualified.json}"
admin_file="/root/ceavpn-admin.env"
reality_file="/root/ceavpn-reality.env"
xhttp_file="/root/ceavpn-xhttp.env"
lte_exit_file="/root/ceavpn-lte-exit.env"
canary_state_file="/root/ceavpn-whitelist-canary.json"
canary_uri_file="/root/ceavpn-whitelist-canary.txt"
isolation_state_file="/root/ceavpn-whitelist-user-isolation.json"
worker_service="ceavpn-worker.service"
worker_reconciled_marker="/run/ceavpn-worker/reconciled"
worker_reconciled_max_age_seconds=120
public_status_dir="/var/www/ceavpn-whitelist"
public_status_file="${public_status_dir}/status.json"
gate_lock_file="/run/lock/ceavpn-whitelist-gate.lock"
ingress_closed_marker="/run/ceavpn-whitelist-ingress-closed"
compose_file="/opt/marzban/docker-compose.yml"
probe_path="/.well-known/ceavpn-whitelist-probe"
confirmation_phrase="restricted-sim-xhttp-tunnel-worked"
canary_inbound_tag="VLESS XHTTP REALITY"
canary_lifetime_seconds=2700
canary_data_limit=104857600
canary_minimum_usage=1048576
api_base="http://127.0.0.1:8000"
work_dir=""
token=""
rollback_canary_username=""
canary_state_tmp=""
canary_uri_tmp=""
qualification_fingerprint=""
public_profile_fingerprint=""
qualification_valid_until=""
public_status_tmp=""
gate_lock_fd="8"
relay_probe_pid=""

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

# This boot path intentionally runs before loading any node configuration.
# A missing or damaged environment file must never preserve a persisted
# public allow rule from the previous boot.
if [[ "${1:-}" == "boot-firewall-close" ]]; then
  umask 077
  boot_failed=0
  boot_ufw_status=""
  rm -f -- "$ingress_closed_marker"
  if ! command -v ufw >/dev/null 2>&1; then
    echo "missing required command: ufw" >&2
    boot_failed=1
  else
    ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
    ufw --force delete deny 443/tcp >/dev/null 2>&1 || true
    if ! ufw insert 1 deny 443/tcp >/dev/null; then
      boot_failed=1
    fi
    if command -v conntrack >/dev/null 2>&1; then
      conntrack -D -p tcp --dport 443 >/dev/null 2>&1 || true
      conntrack -D -p udp --dport 443 >/dev/null 2>&1 || true
    fi
    if ! boot_ufw_status="$(LC_ALL=C ufw status 2>/dev/null)" ||
      ! grep -qx 'Status: active' <<<"$boot_ufw_status" ||
      ! grep -Eq \
        '^[[:space:]]*443/tcp([[:space:]]|$).*DENY' \
        <<<"$boot_ufw_status" ||
      grep -Eq \
        '^[[:space:]]*443/tcp([[:space:]]|$).*ALLOW' \
        <<<"$boot_ufw_status"; then
      boot_failed=1
    fi
  fi
  if ! rm -f -- "$public_status_file"; then
    boot_failed=1
  fi
  if (( boot_failed )); then
    echo "boot-time whitelist firewall close failed" >&2
    exit 1
  fi
  install -o root -g root -m 0600 /dev/null "$ingress_closed_marker"
  exit 0
fi

if [[ ! -s "$node_file" ]]; then
  echo "missing required file: $node_file" >&2
  exit 1
fi

umask 077
# shellcheck disable=SC1090
source "$node_file"

if [[ "${CEAVPN_NODE_MODE:-direct}" != "whitelist" ]]; then
  echo "qualification is available only on a whitelist candidate" >&2
  exit 1
fi
: "${CEAVPN_COVER_DOMAIN:?CEAVPN_COVER_DOMAIN is required}"
: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
: "${CEAVPN_PUBLIC_IP:?CEAVPN_PUBLIC_IP is required}"
: "${CEAVPN_SERVER_CODE:?CEAVPN_SERVER_CODE is required}"
if [[ ! "$CEAVPN_SERVER_CODE" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
  echo "invalid CEAVPN_SERVER_CODE" >&2
  exit 1
fi

if [[ "$CEAVPN_COVER_DOMAIN" == "$CEAVPN_SUB_DOMAIN" ]]; then
  echo "whitelist REALITY camouflage must be a remote domain" >&2
  exit 1
fi
probe_url="https://${CEAVPN_SUB_DOMAIN}:8443${probe_path}"

if ! command -v flock >/dev/null 2>&1; then
  echo "missing required command: flock" >&2
  exit 1
fi
install -d -o root -g root -m 0755 "$(dirname "$gate_lock_file")"
exec 8>"$gate_lock_file"
chmod 0600 "$gate_lock_file"
flock -x "$gate_lock_fd"
export CEAVPN_WHITELIST_GATE_LOCK_FD="$gate_lock_fd"

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -n "$rollback_canary_username" && -n "$token" && -n "$work_dir" ]]; then
    api_request DELETE "/api/user/$rollback_canary_username" \
      "$work_dir/rollback-canary.json" >/dev/null
    rm -f -- "$canary_uri_file"
    if [[ -s "$canary_state_file" ]] &&
      jq -e --arg username "$rollback_canary_username" \
        '.username == $username' "$canary_state_file" >/dev/null 2>&1; then
      rm -f -- "$canary_state_file"
    fi
  fi
  unset MARZBAN_BOT_USERNAME MARZBAN_BOT_PASSWORD
  unset REALITY_PUBLIC_KEY REALITY_SHORT_ID XHTTP_PATH
  unset CEAVPN_LTE_EXIT_ADDRESS CEAVPN_LTE_EXIT_PORT CEAVPN_LTE_EXIT_UUID
  unset CEAVPN_LTE_EXIT_SNI CEAVPN_LTE_EXIT_HOST CEAVPN_LTE_EXIT_PATH
  unset CEAVPN_QUALIFICATION_STATE CEAVPN_QUALIFICATION_OPERATOR
  unset CEAVPN_QUALIFICATION_REGION CEAVPN_QUALIFICATION_PROBE_URL
  unset CEAVPN_QUALIFICATION_OUTPUT CEAVPN_CANARY_STATE
  unset CEAVPN_SERVER_CODE
  unset CEAVPN_CANARY_RESPONSE CEAVPN_CANARY_URI_OUTPUT
  unset CEAVPN_CANARY_STATE_OUTPUT
  unset CEAVPN_WHITELIST_GATE_LOCK_FD
  if [[ -n "$canary_state_tmp" ]]; then
    rm -f -- "$canary_state_tmp"
  fi
  if [[ -n "$canary_uri_tmp" ]]; then
    rm -f -- "$canary_uri_tmp"
  fi
  if [[ -n "$public_status_tmp" ]]; then
    rm -f -- "$public_status_tmp"
  fi
  if [[ -n "$relay_probe_pid" ]]; then
    kill "$relay_probe_pid" >/dev/null 2>&1 || true
    wait "$relay_probe_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    find "$work_dir" -type f -delete
    rmdir "$work_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

usage() {
  cat <<EOF
Usage:
  $0 probe
  $0 status
  $0 canary-create
  $0 canary-status
  $0 canary-revoke
  $0 pass --operator NAME --region NAME --confirm ${confirmation_phrase}
  $0 revoke
  $0 enforce

The pass command is a manual gate. The HTTPS probe is only a prerequisite.
Run pass only after the actual XHTTP/Reality profile worked from the affected
mobile SIM during restrictions: DNS, Telegram, external HTTPS and more than
1 MiB of traffic must all pass through the tunnel. Ping or the probe alone is
not proof.

canary-create creates a 45-minute, 100-MiB XHTTP-only Marzban user. Its VLESS
URI is written to ${canary_uri_file} with mode 0600 and is never printed.
EOF
}

validate_label() {
  local value="$1"
  [[ "$value" =~ ^[[:alnum:]][[:alnum:]\ .,_+-]{0,79}$ ]]
}

local_probe() {
  local body
  body="$(
    curl --noproxy '*' --fail --silent --show-error \
      --connect-timeout 5 --max-time 10 \
      --resolve "${CEAVPN_SUB_DOMAIN}:8443:127.0.0.1" \
      "$probe_url"
  )"
  [[ "$body" == '{"service":"ceavpn","status":"candidate"}' ]]
}

xray_inbound_is_healthy() {
  local xray="/var/lib/marzban/xray-core/xray"
  local active_config="/var/lib/marzban/xray_config.json"
  local listeners xray_processes
  if [[ ! -x "$xray" || ! -s "$active_config" ||
    ! -s "$compose_file" ]]; then
    echo "active whitelist Xray runtime files are missing" >&2
    return 1
  fi
  if ! jq -e --arg tag "$canary_inbound_tag" '
      [
        .inbounds[]
        | select(
            .tag == $tag and
            .listen == "0.0.0.0" and
            .port == 443 and
            .protocol == "vless" and
            .streamSettings.network == "xhttp" and
            .streamSettings.security == "reality"
          )
      ] | length == 1
    ' "$active_config" >/dev/null ||
    ! "$xray" run -test -c "$active_config" >/dev/null 2>&1; then
    echo "active whitelist Xray configuration is invalid" >&2
    return 1
  fi
  if [[ -z "$(
      docker compose -f "$compose_file" ps --status running -q marzban \
        2>/dev/null
    )" ]]; then
    echo "Marzban container is not running" >&2
    return 1
  fi
  if ! xray_processes="$(
    docker top ceavpn-marzban -eo args 2>/dev/null
  )" ||
    ! grep -Fq '/var/lib/marzban/xray-core/xray' <<<"$xray_processes"; then
    echo "active whitelist Xray process is missing" >&2
    return 1
  fi
  if ! listeners="$(ss -H -lnt 2>/dev/null)" ||
    ! grep -Eq \
      '[[:space:]](0\.0\.0\.0|\*|\[::\]):443[[:space:]]' \
      <<<"$listeners"; then
    echo "active whitelist Xray inbound is not ready on port 443" >&2
    return 1
  fi
}

remote_cover_is_healthy() {
  local cover_headers="$work_dir/cover-headers.txt"
  local cover_http_code cover_tls_probe
  if ! timeout 10 env \
    CEAVPN_REMOTE_COVER_DOMAIN="$CEAVPN_COVER_DOMAIN" \
    CEAVPN_REMOTE_INGRESS_IP="$CEAVPN_PUBLIC_IP" \
    python3 - <<'PY'
import ipaddress
import os
import socket

ingress = ipaddress.ip_address(os.environ["CEAVPN_REMOTE_INGRESS_IP"])
addresses = {
    ipaddress.ip_address(item[4][0])
    for item in socket.getaddrinfo(
        os.environ["CEAVPN_REMOTE_COVER_DOMAIN"],
        443,
        type=socket.SOCK_STREAM,
    )
}
if (
    not addresses
    or ingress in addresses
    or any(not address.is_global for address in addresses)
):
    raise SystemExit(1)
PY
  then
    echo "remote REALITY camouflage DNS health failed" >&2
    return 1
  fi
  if ! cover_tls_probe="$(
    timeout 15 openssl s_client \
      -connect "${CEAVPN_COVER_DOMAIN}:443" \
      -servername "$CEAVPN_COVER_DOMAIN" \
      -verify_return_error \
      -verify_hostname "$CEAVPN_COVER_DOMAIN" \
      -tls1_3 -alpn h2 </dev/null 2>&1
  )" ||
    ! grep -q 'ALPN protocol: h2' <<<"$cover_tls_probe" ||
    ! grep -q 'Verify return code: 0 (ok)' <<<"$cover_tls_probe"; then
    echo "remote REALITY camouflage TLS1.3/HTTP2 health failed" >&2
    return 1
  fi
  if ! cover_http_code="$(
    curl --noproxy '*' --silent --show-error \
      --proto '=https' --proto-redir '=https' \
      --http2 --tlsv1.3 --max-redirs 0 \
      --connect-timeout 5 --max-time 15 \
      --dump-header "$cover_headers" --output /dev/null \
      --write-out '%{http_code}' \
      "https://${CEAVPN_COVER_DOMAIN}/"
  )" ||
    [[ ! "$cover_http_code" =~ ^[24][0-9]{2}$ ]] ||
    grep -qi '^location:' "$cover_headers"; then
    echo "remote REALITY camouflage HTTP health failed" >&2
    return 1
  fi
}

get_token() {
  python3 - <<'PY' |
import os
import urllib.parse

print(urllib.parse.urlencode({
    "username": os.environ["MARZBAN_BOT_USERNAME"],
    "password": os.environ["MARZBAN_BOT_PASSWORD"],
    "grant_type": "password",
}))
PY
  curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 5 --max-time 20 \
    -X POST "$api_base/api/admin/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-binary @- |
    jq -er '.access_token | select(type == "string" and length > 0)'
}

api_request() {
  local method="$1"
  local path="$2"
  local output="$3"
  local payload="${4:-}"
  local args=(
    --noproxy '*' --silent --show-error --connect-timeout 5 --max-time 20
    -o "$output" -w '%{http_code}' -X "$method"
    -H "Authorization: Bearer $token"
  )
  if [[ -n "$payload" ]]; then
    args+=(-H 'Content-Type: application/json' --data-binary "@$payload")
  fi
  curl "${args[@]}" "$api_base$path"
}

load_gate_context() {
  local path
  for path in "$reality_file" "$xhttp_file" "$lte_exit_file"; do
    if [[ ! -s "$path" ]]; then
      echo "missing required whitelist gate file: $path" >&2
      return 1
    fi
  done
  for command in jq python3 docker ss timeout openssl curl; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "missing required command: $command" >&2
      return 1
    fi
  done
  if [[ -z "$work_dir" ]]; then
    work_dir="$(mktemp -d /run/ceavpn-whitelist-canary.XXXXXX)"
    chmod 0700 "$work_dir"
  fi
  # shellcheck disable=SC1090
  source "$reality_file"
  # shellcheck disable=SC1090
  source "$xhttp_file"
  # shellcheck disable=SC1090
  source "$lte_exit_file"
  : "${REALITY_PUBLIC_KEY:?REALITY_PUBLIC_KEY is required}"
  : "${REALITY_SHORT_ID:?REALITY_SHORT_ID is required}"
  : "${XHTTP_PATH:?XHTTP_PATH is required}"
  : "${CEAVPN_LTE_EXIT_ADDRESS:?CEAVPN_LTE_EXIT_ADDRESS is required}"
  : "${CEAVPN_LTE_EXIT_PORT:?CEAVPN_LTE_EXIT_PORT is required}"
  : "${CEAVPN_LTE_EXIT_UUID:?CEAVPN_LTE_EXIT_UUID is required}"
  : "${CEAVPN_LTE_EXIT_SNI:?CEAVPN_LTE_EXIT_SNI is required}"
  : "${CEAVPN_LTE_EXIT_HOST:?CEAVPN_LTE_EXIT_HOST is required}"
  : "${CEAVPN_LTE_EXIT_PATH:?CEAVPN_LTE_EXIT_PATH is required}"
  if [[ ! "$REALITY_PUBLIC_KEY" =~ ^[A-Za-z0-9_-]{43}$ ]] ||
    [[ ! "$REALITY_SHORT_ID" =~ ^[0-9A-Fa-f]{16}$ ]] ||
    [[ ! "$XHTTP_PATH" =~ ^/xhttp-[0-9a-f]{48}$ ]]; then
    echo "invalid canary transport secrets" >&2
    return 1
  fi
}

prepare_canary_api() {
  if ! load_gate_context; then
    return 1
  fi
  if [[ ! -s "$admin_file" ]]; then
    echo "missing required canary file: $admin_file" >&2
    return 1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "missing required command: curl" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  source "$admin_file"
  : "${MARZBAN_BOT_USERNAME:?MARZBAN_BOT_USERNAME is required}"
  : "${MARZBAN_BOT_PASSWORD:?MARZBAN_BOT_PASSWORD is required}"
  export MARZBAN_BOT_USERNAME MARZBAN_BOT_PASSWORD
  if ! token="$(get_token)"; then
    unset MARZBAN_BOT_PASSWORD
    echo "could not authenticate to the local Marzban API" >&2
    return 1
  fi
  unset MARZBAN_BOT_PASSWORD
}

compute_config_fingerprint() {
  CEAVPN_FINGERPRINT_PUBLIC_IP="$CEAVPN_PUBLIC_IP" \
  CEAVPN_FINGERPRINT_SERVER_CODE="$CEAVPN_SERVER_CODE" \
  CEAVPN_FINGERPRINT_COVER_DOMAIN="$CEAVPN_COVER_DOMAIN" \
  CEAVPN_FINGERPRINT_SUB_DOMAIN="$CEAVPN_SUB_DOMAIN" \
  CEAVPN_FINGERPRINT_XHTTP_PATH="$XHTTP_PATH" \
  CEAVPN_FINGERPRINT_REALITY_PUBLIC_KEY="$REALITY_PUBLIC_KEY" \
  CEAVPN_FINGERPRINT_REALITY_SHORT_ID="$REALITY_SHORT_ID" \
  CEAVPN_FINGERPRINT_EXIT_ADDRESS="$CEAVPN_LTE_EXIT_ADDRESS" \
  CEAVPN_FINGERPRINT_EXIT_PORT="$CEAVPN_LTE_EXIT_PORT" \
  CEAVPN_FINGERPRINT_EXIT_UUID="$CEAVPN_LTE_EXIT_UUID" \
  CEAVPN_FINGERPRINT_EXIT_SNI="$CEAVPN_LTE_EXIT_SNI" \
  CEAVPN_FINGERPRINT_EXIT_HOST="$CEAVPN_LTE_EXIT_HOST" \
  CEAVPN_FINGERPRINT_EXIT_PATH="$CEAVPN_LTE_EXIT_PATH" \
    python3 - <<'PY'
import hashlib
import ipaddress
import json
import os
import re
import uuid

public_ip = str(ipaddress.ip_address(os.environ["CEAVPN_FINGERPRINT_PUBLIC_IP"]))
try:
    exit_port = int(os.environ["CEAVPN_FINGERPRINT_EXIT_PORT"])
except ValueError as exc:
    raise SystemExit("invalid fingerprint exit port") from exc
if not 1 <= exit_port <= 65535:
    raise SystemExit("invalid fingerprint exit port")
exit_uuid = str(uuid.UUID(os.environ["CEAVPN_FINGERPRINT_EXIT_UUID"]))
xhttp_path = os.environ["CEAVPN_FINGERPRINT_XHTTP_PATH"]
exit_path = os.environ["CEAVPN_FINGERPRINT_EXIT_PATH"]
if (
    re.fullmatch(r"/xhttp-[0-9a-f]{48}", xhttp_path) is None
    or re.fullmatch(r"/ws-[0-9a-f]{48}", exit_path) is None
):
    raise SystemExit("invalid fingerprint transport path")
payload = {
    "server_code": os.environ["CEAVPN_FINGERPRINT_SERVER_CODE"],
    "public_ip": public_ip,
    "cover_domain": os.environ["CEAVPN_FINGERPRINT_COVER_DOMAIN"],
    "subscription_domain": os.environ["CEAVPN_FINGERPRINT_SUB_DOMAIN"],
    "xhttp_path": xhttp_path,
    "reality_public_key": os.environ["CEAVPN_FINGERPRINT_REALITY_PUBLIC_KEY"],
    "reality_short_id": os.environ["CEAVPN_FINGERPRINT_REALITY_SHORT_ID"],
    "exit_address": os.environ["CEAVPN_FINGERPRINT_EXIT_ADDRESS"],
    "exit_port": exit_port,
    "exit_uuid": exit_uuid,
    "exit_sni": os.environ["CEAVPN_FINGERPRINT_EXIT_SNI"],
    "exit_host": os.environ["CEAVPN_FINGERPRINT_EXIT_HOST"],
    "exit_path": exit_path,
}
encoded = json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("ascii")
print(hashlib.sha256(encoded).hexdigest())
PY
}

compute_public_profile_fingerprint() {
  CEAVPN_PROFILE_ADDRESS="$CEAVPN_PUBLIC_IP" \
  CEAVPN_PROFILE_SERVER_CODE="$CEAVPN_SERVER_CODE" \
  CEAVPN_PROFILE_PATH="$XHTTP_PATH" \
  CEAVPN_PROFILE_SNI="$CEAVPN_COVER_DOMAIN" \
  CEAVPN_PROFILE_QUALIFICATION_HOST="$CEAVPN_SUB_DOMAIN" \
  CEAVPN_PROFILE_PUBLIC_KEY="$REALITY_PUBLIC_KEY" \
  CEAVPN_PROFILE_SHORT_ID="$REALITY_SHORT_ID" \
    python3 - <<'PY'
import hashlib
import ipaddress
import json
import os

sni = os.environ["CEAVPN_PROFILE_SNI"]
qualification_host = os.environ["CEAVPN_PROFILE_QUALIFICATION_HOST"]
xhttp_extra = {
    "scMaxEachPostBytes": 1000000,
    "scMaxConcurrentPosts": 100,
    "scMinPostsIntervalMs": 30,
    "xPaddingBytes": "100-1000",
    "noGRPCHeader": False,
}
payload = {
    "server_code": os.environ["CEAVPN_PROFILE_SERVER_CODE"],
    "address": str(ipaddress.ip_address(os.environ["CEAVPN_PROFILE_ADDRESS"])),
    "port": 443,
    "transport": "xhttp",
    "security": "reality",
    "path": os.environ["CEAVPN_PROFILE_PATH"],
    "sni": sni,
    "pbk": os.environ["CEAVPN_PROFILE_PUBLIC_KEY"],
    "sid": os.environ["CEAVPN_PROFILE_SHORT_ID"],
    "fingerprint": "chrome",
    "mode": "auto",
    "extra": xhttp_extra,
    "qualification_url": (
        f"https://{qualification_host}:8443"
        "/.well-known/ceavpn-whitelist-status"
    ),
}
encoded = json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("ascii")
print(hashlib.sha256(encoded).hexdigest())
PY
}

relay_e2e_is_healthy() {
  local xray="/var/lib/marzban/xray-core/xray"
  local probe_port=19081
  local config="$work_dir/relay-e2e.json"
  local ready=0
  if [[ ! -x "$xray" ]]; then
    echo "missing pinned Xray for relay health check" >&2
    return 1
  fi
  CEAVPN_RELAY_PROBE_ADDRESS="$CEAVPN_LTE_EXIT_ADDRESS" \
  CEAVPN_RELAY_PROBE_PORT="$CEAVPN_LTE_EXIT_PORT" \
  CEAVPN_RELAY_PROBE_UUID="$CEAVPN_LTE_EXIT_UUID" \
  CEAVPN_RELAY_PROBE_SNI="$CEAVPN_LTE_EXIT_SNI" \
  CEAVPN_RELAY_PROBE_HOST="$CEAVPN_LTE_EXIT_HOST" \
  CEAVPN_RELAY_PROBE_PATH="$CEAVPN_LTE_EXIT_PATH" \
  CEAVPN_RELAY_PROBE_CONFIG="$config" \
  CEAVPN_RELAY_PROBE_LOCAL_PORT="$probe_port" \
    python3 - <<'PY'
import json
import os
import uuid
from pathlib import Path

payload = {
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": int(os.environ["CEAVPN_RELAY_PROBE_LOCAL_PORT"]),
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
    }],
    "outbounds": [{
        "tag": "RELAY",
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": os.environ["CEAVPN_RELAY_PROBE_ADDRESS"],
                "port": int(os.environ["CEAVPN_RELAY_PROBE_PORT"]),
                "users": [{
                    "id": str(uuid.UUID(os.environ["CEAVPN_RELAY_PROBE_UUID"])),
                    "encryption": "none",
                }],
            }],
        },
        "streamSettings": {
            "network": "ws",
            "security": "tls",
            "tlsSettings": {
                "serverName": os.environ["CEAVPN_RELAY_PROBE_SNI"],
                "allowInsecure": False,
                "fingerprint": "chrome",
                "alpn": ["http/1.1"],
            },
            "wsSettings": {
                "path": os.environ["CEAVPN_RELAY_PROBE_PATH"],
                "headers": {"Host": os.environ["CEAVPN_RELAY_PROBE_HOST"]},
            },
        },
    }],
}
Path(os.environ["CEAVPN_RELAY_PROBE_CONFIG"]).write_text(
    json.dumps(payload, separators=(",", ":")),
    encoding="utf-8",
)
PY
  chmod 0600 "$config"
  if ! "$xray" run -test -c "$config" >/dev/null 2>&1; then
    echo "relay health-check Xray config is invalid" >&2
    return 1
  fi
  "$xray" run -c "$config" >"$work_dir/relay-e2e.log" 2>&1 &
  relay_probe_pid="$!"
  for _ in {1..10}; do
    if ! kill -0 "$relay_probe_pid" >/dev/null 2>&1; then
      break
    fi
    if curl --noproxy '' --fail --silent --show-error \
      --socks5-hostname "127.0.0.1:${probe_port}" \
      --connect-timeout 1 --max-time 3 --max-filesize 4096 \
      -o /dev/null \
      https://www.cloudflare.com/cdn-cgi/trace; then
      ready=1
      break
    fi
    sleep 0.25
  done
  kill "$relay_probe_pid" >/dev/null 2>&1 || true
  wait "$relay_probe_pid" >/dev/null 2>&1 || true
  relay_probe_pid=""
  if (( ! ready )); then
    echo "foreign relay or external HTTPS egress check failed" >&2
    return 1
  fi
}

ufw_status() {
  LC_ALL=C ufw status 2>/dev/null
}

public_ingress_is_open() {
  local status
  status="$(ufw_status)" || return 1
  grep -qx 'Status: active' <<<"$status" &&
    grep -Eq '^[[:space:]]*443/tcp([[:space:]]|$).*ALLOW' <<<"$status" &&
    ! grep -Eq '^[[:space:]]*443/tcp([[:space:]]|$).*DENY' <<<"$status"
}

public_ingress_is_closed() {
  local status
  status="$(ufw_status)" || return 1
  grep -qx 'Status: active' <<<"$status" &&
    grep -Eq '^[[:space:]]*443/tcp([[:space:]]|$).*DENY' <<<"$status" &&
    ! grep -Eq '^[[:space:]]*443/tcp([[:space:]]|$).*ALLOW' <<<"$status"
}

open_public_ingress() {
  if ! command -v ufw >/dev/null 2>&1; then
    echo "missing required command: ufw" >&2
    return 1
  fi
  rm -f -- "$ingress_closed_marker"
  ufw --force delete deny 443/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null
  if ! public_ingress_is_open; then
    echo "public ingress firewall rule did not open" >&2
    return 1
  fi
}

close_public_ingress() {
  local hard_stop_required=1
  local failed=0
  if ! command -v ufw >/dev/null 2>&1; then
    echo "missing required command: ufw" >&2
    return 1
  fi
  if [[ -f "$ingress_closed_marker" ]] && public_ingress_is_closed; then
    hard_stop_required=0
  fi
  ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
  ufw --force delete deny 443/tcp >/dev/null 2>&1 || true
  if ! ufw insert 1 deny 443/tcp >/dev/null; then
    failed=1
  fi
  if (( hard_stop_required )); then
    if command -v conntrack >/dev/null 2>&1; then
      conntrack -D -p tcp --dport 443 >/dev/null 2>&1 || true
      conntrack -D -p udp --dport 443 >/dev/null 2>&1 || true
    else
      echo "missing required command: conntrack" >&2
      failed=1
    fi
    if [[ ! -s "$compose_file" ]] ||
      ! docker compose -f "$compose_file" restart marzban >/dev/null; then
      echo "could not restart Marzban/Xray to terminate active sessions" >&2
      failed=1
    fi
  fi
  if ! public_ingress_is_closed; then
    echo "public ingress firewall rule did not close" >&2
    failed=1
  fi
  if (( failed )); then
    rm -f -- "$ingress_closed_marker"
    return 1
  fi
  install -o root -g root -m 0600 /dev/null "$ingress_closed_marker"
}

close_public_firewall_at_boot() {
  local failed=0
  if ! command -v ufw >/dev/null 2>&1; then
    echo "missing required command: ufw" >&2
    return 1
  fi
  ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
  ufw --force delete deny 443/tcp >/dev/null 2>&1 || true
  if ! ufw insert 1 deny 443/tcp >/dev/null; then
    failed=1
  fi
  if command -v conntrack >/dev/null 2>&1; then
    conntrack -D -p tcp --dport 443 >/dev/null 2>&1 || true
    conntrack -D -p udp --dport 443 >/dev/null 2>&1 || true
  fi
  if ! public_ingress_is_closed; then
    failed=1
  fi
  if (( failed )); then
    rm -f -- "$ingress_closed_marker"
    return 1
  fi
  install -o root -g root -m 0600 /dev/null "$ingress_closed_marker"
}

remove_public_status() {
  rm -f -- "$public_status_file" || return 1
  [[ ! -e "$public_status_file" ]]
}

force_gate_closed() {
  local failed=0
  if ! close_public_ingress; then
    failed=1
  fi
  if ! remove_public_status; then
    echo "could not remove public qualification status" >&2
    failed=1
  fi
  return "$failed"
}

publish_public_status() {
  local internal_fingerprint="$1"
  local profile_fingerprint="$2"
  if ! install -d -o root -g root -m 0755 "$public_status_dir" ||
    ! public_status_tmp="$(mktemp "${public_status_file}.new.XXXXXX")"; then
    echo "could not prepare public qualification status" >&2
    return 1
  fi
  if ! CEAVPN_QUALIFICATION_FILE="$qualification_file" \
    CEAVPN_QUALIFICATION_EXPECTED_FINGERPRINT="$internal_fingerprint" \
    CEAVPN_PUBLIC_PROFILE_FINGERPRINT="$profile_fingerprint" \
    CEAVPN_PUBLIC_STATUS_OUTPUT="$public_status_tmp" \
    python3 - <<'PY'
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

record = json.loads(
    Path(os.environ["CEAVPN_QUALIFICATION_FILE"]).read_text(encoding="utf-8")
)
valid_until = datetime.fromisoformat(
    record["valid_until"].replace("Z", "+00:00")
)
if valid_until.tzinfo is None:
    valid_until = valid_until.replace(tzinfo=timezone.utc)
valid_until = valid_until.astimezone(timezone.utc)
if (
    record.get("status") != "passed"
    or record.get("config_fingerprint")
    != os.environ["CEAVPN_QUALIFICATION_EXPECTED_FINGERPRINT"]
    or valid_until <= datetime.now(timezone.utc)
    or re.fullmatch(
        r"[0-9a-f]{64}", os.environ["CEAVPN_PUBLIC_PROFILE_FINGERPRINT"]
    )
    is None
):
    raise SystemExit("qualification is not publishable")
payload = {
    "service": "ceavpn-whitelist-gate-v1",
    "status": "passed",
    "config_fingerprint": os.environ["CEAVPN_PUBLIC_PROFILE_FINGERPRINT"],
    "valid_until": valid_until.replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    ),
}
Path(os.environ["CEAVPN_PUBLIC_STATUS_OUTPUT"]).write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  then
    echo "qualification is not publishable" >&2
    return 1
  fi
  if ! chown root:root "$public_status_tmp" ||
    ! chmod 0644 "$public_status_tmp" ||
    ! mv "$public_status_tmp" "$public_status_file"; then
    echo "could not publish public qualification status" >&2
    return 1
  fi
  public_status_tmp=""
}

qualification_record_is_current() {
  local fingerprint="$1"
  [[ -s "$qualification_file" ]] || return 1
  CEAVPN_QUALIFICATION_FILE="$qualification_file" \
  CEAVPN_QUALIFICATION_EXPECTED_FINGERPRINT="$fingerprint" \
  CEAVPN_QUALIFICATION_EXPECTED_PROBE="$probe_url" \
    python3 - <<'PY'
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    payload = json.loads(
        Path(os.environ["CEAVPN_QUALIFICATION_FILE"]).read_text(
            encoding="utf-8"
        )
    )
    valid_until = datetime.fromisoformat(
        payload["valid_until"].replace("Z", "+00:00")
    )
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if valid_until.tzinfo is None:
    valid_until = valid_until.replace(tzinfo=timezone.utc)
required_checks = {
    "xhttp_profile_connected",
    "dns_through_tunnel",
    "telegram_through_tunnel",
    "https_through_tunnel",
    "transfer_over_1mib",
}
allowed = {
    "status",
    "recorded_at",
    "valid_until",
    "operator",
    "region",
    "probe_url",
    "config_fingerprint",
    "evidence",
    "checks",
}
valid = (
    isinstance(payload, dict)
    and set(payload) == allowed
    and all(
        isinstance(payload[key], str)
        for key in allowed - {"checks"}
    )
    and isinstance(payload["checks"], list)
    and all(isinstance(item, str) for item in payload["checks"])
    and payload["status"] == "passed"
    and payload["evidence"] == "restricted-sim-xhttp-tunnel-worked"
    and re.fullmatch(r"[0-9a-f]{64}", payload["config_fingerprint"])
    is not None
    and payload["config_fingerprint"]
    == os.environ["CEAVPN_QUALIFICATION_EXPECTED_FINGERPRINT"]
    and payload["probe_url"]
    == os.environ["CEAVPN_QUALIFICATION_EXPECTED_PROBE"]
    and set(payload["checks"]) == required_checks
    and len(payload["checks"]) == len(required_checks)
    and valid_until.astimezone(timezone.utc) > datetime.now(timezone.utc)
)
raise SystemExit(0 if valid else 1)
PY
}

canary_window_is_active() {
  validate_canary_state active || return 1
  jq -e --argjson now "$(date -u +%s)" \
    '.expires_at > $now' "$canary_state_file" >/dev/null
}

validate_canary_state() {
  local allowed_status="${1:-active}"
  [[ -s "$canary_state_file" ]] || return 1
  CEAVPN_CANARY_STATE="$canary_state_file" \
  CEAVPN_CANARY_ALLOWED_STATUS="$allowed_status" \
    python3 - <<'PY'
import json
import os
import re
import uuid
from pathlib import Path

payload = json.loads(
    Path(os.environ["CEAVPN_CANARY_STATE"]).read_text(encoding="utf-8")
)
allowed_status = os.environ["CEAVPN_CANARY_ALLOWED_STATUS"]
status = payload.get("status")
if allowed_status == "any-audit" and status in {"revoked", "consumed"}:
    expected = {"status", "recorded_at", "username"}
    if (
        set(payload) != expected
        or not all(isinstance(payload[key], str) for key in expected)
        or not re.fullmatch(r"cea_canary_[0-9a-f]{12}", payload["username"])
    ):
        raise SystemExit(1)
    raise SystemExit(0)
if status != allowed_status or status != "active":
    raise SystemExit(1)
expected = {
    "status",
    "username",
    "uuid",
    "created_at",
    "expires_at",
    "data_limit",
}
if set(payload) != expected:
    raise SystemExit(1)
if not re.fullmatch(r"cea_canary_[0-9a-f]{12}", payload["username"]):
    raise SystemExit(1)
try:
    value = uuid.UUID(payload["uuid"])
except (TypeError, ValueError):
    raise SystemExit(1)
if value.version != 4:
    raise SystemExit(1)
if (
    not isinstance(payload["created_at"], str)
    or isinstance(payload["expires_at"], bool)
    or not isinstance(payload["expires_at"], int)
    or payload["data_limit"] != 104857600
):
    raise SystemExit(1)
PY
}

isolation_state_is_valid() {
  [[ -s "$isolation_state_file" ]] || return 1
  CEAVPN_ISOLATION_STATE="$isolation_state_file" python3 - <<'PY'
import json
import os
import re
import uuid
from pathlib import Path

payload = json.loads(
    Path(os.environ["CEAVPN_ISOLATION_STATE"]).read_text(encoding="utf-8")
)
if set(payload) != {
    "status",
    "recorded_at",
    "worker_was_active",
    "users",
}:
    raise SystemExit(1)
if (
    payload["status"] not in {"isolating", "isolated", "restored"}
    or not isinstance(payload["recorded_at"], str)
    or not isinstance(payload["worker_was_active"], bool)
    or not isinstance(payload["users"], list)
):
    raise SystemExit(1)
seen = set()
for user in payload["users"]:
    if not isinstance(user, dict) or set(user) != {"username", "uuid"}:
        raise SystemExit(1)
    username = user["username"]
    identifier = user["uuid"]
    if (
        not isinstance(username, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username) is None
        or username in seen
    ):
        raise SystemExit(1)
    try:
        normalized = str(uuid.UUID(identifier))
    except (AttributeError, TypeError, ValueError):
        raise SystemExit(1)
    if normalized != identifier:
        raise SystemExit(1)
    seen.add(username)
PY
}

write_isolation_state() {
  local state="$1"
  local worker_was_active="$2"
  local users_file="$3"
  local output
  output="$(mktemp "${isolation_state_file}.new.XXXXXX")"
  if ! CEAVPN_ISOLATION_STATUS="$state" \
    CEAVPN_ISOLATION_WORKER_WAS_ACTIVE="$worker_was_active" \
    CEAVPN_ISOLATION_USERS="$users_file" \
    CEAVPN_ISOLATION_OUTPUT="$output" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

status = os.environ["CEAVPN_ISOLATION_STATUS"]
if status not in {"isolating", "isolated", "restored"}:
    raise SystemExit("invalid isolation status")
users = json.loads(
    Path(os.environ["CEAVPN_ISOLATION_USERS"]).read_text(encoding="utf-8")
)
payload = {
    "status": status,
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "worker_was_active": (
        os.environ["CEAVPN_ISOLATION_WORKER_WAS_ACTIVE"] == "true"
    ),
    "users": users,
}
Path(os.environ["CEAVPN_ISOLATION_OUTPUT"]).write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  then
    rm -f -- "$output"
    return 1
  fi
  if ! chmod 0600 "$output" || ! mv "$output" "$isolation_state_file"; then
    rm -f -- "$output"
    return 1
  fi
}

worker_reconciliation_is_fresh() {
  local marker_epoch now
  if [[ ! -f "$worker_reconciled_marker" ||
    -L "$worker_reconciled_marker" ]] ||
    [[ "$(stat -c '%a' "$worker_reconciled_marker" 2>/dev/null || true)" != "600" ]] ||
    [[ "$(wc -l <"$worker_reconciled_marker" 2>/dev/null || true)" != "1" ]]; then
    return 1
  fi
  marker_epoch="$(<"$worker_reconciled_marker")"
  if [[ ! "$marker_epoch" =~ ^[0-9]{10}$ ]]; then
    return 1
  fi
  now="$(date -u +%s)"
  (( marker_epoch <= now + 5 )) &&
    (( now - marker_epoch <= worker_reconciled_max_age_seconds ))
}

encoded_username() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
}

list_active_xhttp_users() {
  local output="$1"
  local exclude_username="${2:-}"
  local pages="$work_dir/isolation-pages.jsonl"
  local offset=0
  local limit=100
  local page count total code
  : >"$pages"
  while :; do
    page="$work_dir/isolation-page-${offset}.json"
    code="$(
      api_request GET "/api/users?offset=${offset}&limit=${limit}" "$page"
    )"
    if [[ "$code" != "200" ]] ||
      ! jq -e '
        (.users | type == "array") and
        ((.total | type) == "number") and
        .total >= 0 and
        ((.total | floor) == .total)
      ' "$page" >/dev/null; then
      echo "could not enumerate XHTTP users for canary isolation" >&2
      return 1
    fi
    if ! jq -e \
      --arg tag "$canary_inbound_tag" \
      --arg exclude "$exclude_username" '
        all(
          .users[];
          .username == $exclude or
          .status != "on_hold" or
          (.inbounds.vless | type) != "array" or
          (.inbounds.vless | index($tag)) == null
        )
      ' "$page" >/dev/null; then
      echo "on-hold XHTTP users cannot be safely isolated; ingress stays closed" >&2
      return 1
    fi
    if ! count="$(jq -er '.users | length' "$page")" ||
      ! total="$(jq -er '.total' "$page")"; then
      echo "invalid Marzban user pagination" >&2
      return 1
    fi
    if ! jq -c \
      --arg tag "$canary_inbound_tag" \
      --arg exclude "$exclude_username" '
      [
        .users[]
        | select(
            .username != $exclude and
            .status == "active" and
            (.inbounds.vless | type == "array") and
            (.inbounds.vless | index($tag)) != null
          )
        | {
            username: .username,
            uuid: .proxies.vless.id
          }
      ]
    ' "$page" >>"$pages"; then
      echo "could not parse XHTTP users for canary isolation" >&2
      return 1
    fi
    offset="$((offset + count))"
    if (( offset >= total )); then
      break
    fi
    if (( count == 0 )); then
      echo "invalid Marzban user pagination" >&2
      return 1
    fi
  done
  CEAVPN_ISOLATION_PAGES="$pages" \
  CEAVPN_ISOLATION_USERS_OUTPUT="$output" \
    python3 - <<'PY'
import json
import os
import re
import uuid
from pathlib import Path

users = {}
for line in Path(os.environ["CEAVPN_ISOLATION_PAGES"]).read_text(
    encoding="utf-8"
).splitlines():
    for item in json.loads(line):
        username = item.get("username")
        identifier = item.get("uuid")
        if (
            not isinstance(username, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username) is None
        ):
            raise SystemExit("invalid Marzban username during isolation")
        try:
            normalized = str(uuid.UUID(identifier))
        except (AttributeError, TypeError, ValueError):
            raise SystemExit("invalid Marzban UUID during isolation")
        previous = users.setdefault(username, normalized)
        if previous != normalized:
            raise SystemExit("conflicting Marzban identity during isolation")
Path(os.environ["CEAVPN_ISOLATION_USERS_OUTPUT"]).write_text(
    json.dumps(
        [
            {"username": username, "uuid": users[username]}
            for username in sorted(users)
        ],
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY
}

merge_isolation_users() {
  local existing="$1"
  local discovered="$2"
  local output="$3"
  CEAVPN_ISOLATION_EXISTING="$existing" \
  CEAVPN_ISOLATION_DISCOVERED="$discovered" \
  CEAVPN_ISOLATION_MERGED="$output" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

merged = {}
for variable in ("CEAVPN_ISOLATION_EXISTING", "CEAVPN_ISOLATION_DISCOVERED"):
    for item in json.loads(Path(os.environ[variable]).read_text(encoding="utf-8")):
        previous = merged.setdefault(item["username"], item["uuid"])
        if previous != item["uuid"]:
            raise SystemExit("conflicting isolation identity")
Path(os.environ["CEAVPN_ISOLATION_MERGED"]).write_text(
    json.dumps(
        [
            {"username": username, "uuid": merged[username]}
            for username in sorted(merged)
        ],
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY
}

set_xhttp_user_status() {
  local username="$1"
  local expected_uuid="$2"
  local desired_status="$3"
  local index="$4"
  local encoded current_code current_status update_code
  encoded="$(encoded_username "$username")"
  current_code="$(
    api_request GET "/api/user/$encoded" \
      "$work_dir/isolation-user-${index}.json"
  )"
  if [[ "$current_code" == "404" && "$desired_status" == "active" ]]; then
    return 0
  fi
  if [[ "$current_code" != "200" ]]; then
    echo "could not inspect an isolated XHTTP user" >&2
    return 1
  fi
  if ! jq -e \
    --arg uuid "$expected_uuid" \
    --arg tag "$canary_inbound_tag" '
      .proxies.vless.id == $uuid and
      (.inbounds.vless | type == "array") and
      (.inbounds.vless | index($tag)) != null
    ' "$work_dir/isolation-user-${index}.json" >/dev/null; then
    echo "isolated XHTTP user identity changed" >&2
    return 1
  fi
  current_status="$(
    jq -er '.status' "$work_dir/isolation-user-${index}.json"
  )"
  if [[ "$desired_status" == "disabled" ]]; then
    if [[ "$current_status" == "disabled" ]]; then
      return 0
    fi
    if [[ "$current_status" != "active" ]]; then
      echo "XHTTP user changed state during canary isolation" >&2
      return 1
    fi
  else
    if jq -e --argjson now "$(date -u +%s)" '
      (.expire | type == "number") and .expire > 0 and .expire <= $now
    ' "$work_dir/isolation-user-${index}.json" >/dev/null; then
      return 0
    fi
    if [[ "$current_status" == "active" ||
      "$current_status" == "expired" ||
      "$current_status" == "limited" ]]; then
      return 0
    fi
    if [[ "$current_status" != "disabled" ]]; then
      echo "isolated XHTTP user cannot be safely restored" >&2
      return 1
    fi
  fi
  printf '{"status":"%s"}' "$desired_status" \
    >"$work_dir/isolation-update-${index}.json"
  update_code="$(
    api_request PUT "/api/user/$encoded" \
      "$work_dir/isolation-update-response-${index}.json" \
      "$work_dir/isolation-update-${index}.json"
  )"
  if [[ "$update_code" != "200" ]] ||
    ! jq -e \
      --arg uuid "$expected_uuid" \
      --arg tag "$canary_inbound_tag" \
      --arg status "$desired_status" '
        .proxies.vless.id == $uuid and
        (.inbounds.vless | type == "array") and
        (.inbounds.vless | index($tag)) != null and
        .status == $status
      ' "$work_dir/isolation-update-response-${index}.json" >/dev/null; then
    echo "could not verify isolated XHTTP user state" >&2
    return 1
  fi
}

isolate_non_canary_users() {
  local worker_was_active existing_users discovered_users merged_users
  local canary_username="" index=0 username identifier
  existing_users="$work_dir/isolation-existing.json"
  discovered_users="$work_dir/isolation-discovered.json"
  merged_users="$work_dir/isolation-merged.json"
  if [[ -s "$isolation_state_file" ]]; then
    if ! isolation_state_is_valid; then
      echo "invalid XHTTP isolation state; keeping ingress closed" >&2
      return 1
    fi
    worker_was_active="$(
      jq -r '.worker_was_active | if . then "true" else "false" end' \
        "$isolation_state_file"
    )"
    jq -c '.users' "$isolation_state_file" >"$existing_users"
  else
    worker_was_active="false"
    if systemctl is-active --quiet "$worker_service"; then
      worker_was_active="true"
    fi
    printf '[]' >"$existing_users"
    if ! write_isolation_state \
      "isolating" "$worker_was_active" "$existing_users"; then
      return 1
    fi
  fi
  if ! systemctl stop "$worker_service" ||
    systemctl is-active --quiet "$worker_service"; then
    echo "could not stop VPN worker for canary isolation" >&2
    return 1
  fi
  if validate_canary_state active >/dev/null 2>&1; then
    canary_username="$(jq -er '.username' "$canary_state_file")"
  fi
  if ! list_active_xhttp_users "$discovered_users" "$canary_username"; then
    return 1
  fi
  if ! merge_isolation_users \
    "$existing_users" "$discovered_users" "$merged_users"; then
    return 1
  fi
  if ! write_isolation_state \
    "isolating" "$worker_was_active" "$merged_users"; then
    return 1
  fi
  while IFS=$'\t' read -r username identifier; do
    index="$((index + 1))"
    if ! set_xhttp_user_status \
      "$username" "$identifier" "disabled" "$index"; then
      return 1
    fi
  done < <(jq -r '.[] | [.username, .uuid] | @tsv' "$merged_users")
  write_isolation_state "isolated" "$worker_was_active" "$merged_users"
}

restore_isolated_users() {
  local worker_was_active worker_reconciled restored_users
  local index=0 username identifier
  if [[ ! -e "$isolation_state_file" ]]; then
    return 0
  fi
  if ! public_ingress_is_closed ||
    [[ ! -f "$ingress_closed_marker" ]]; then
    echo "refusing to restore XHTTP users before verified hard-stop" >&2
    return 1
  fi
  if ! isolation_state_is_valid; then
    echo "invalid XHTTP isolation state; manual recovery required" >&2
    return 1
  fi
  worker_was_active="$(
    jq -r '.worker_was_active | if . then "true" else "false" end' \
      "$isolation_state_file"
  )"
  while IFS=$'\t' read -r username identifier; do
    index="$((index + 1))"
    if ! set_xhttp_user_status \
      "$username" "$identifier" "active" "restore-${index}"; then
      return 1
    fi
  done < <(
    jq -r '.users[] | [.username, .uuid] | @tsv' "$isolation_state_file"
  )
  if [[ "$worker_was_active" == "true" ]]; then
    rm -f -- "$worker_reconciled_marker"
    if ! systemctl start "$worker_service" ||
      ! systemctl is-active --quiet "$worker_service"; then
      echo "could not restart VPN worker after canary isolation" >&2
      return 1
    fi
    worker_reconciled=0
    for _ in {1..60}; do
      if worker_reconciliation_is_fresh; then
        worker_reconciled=1
        break
      fi
      sleep 1
    done
    if (( ! worker_reconciled )); then
      echo "VPN worker did not finish reconciliation; ingress stays closed" >&2
      return 1
    fi
  fi
  restored_users="$work_dir/isolation-restored-users.json"
  if ! jq -c '.users' "$isolation_state_file" >"$restored_users" ||
    ! write_isolation_state \
      "restored" "$worker_was_active" "$restored_users"; then
    return 1
  fi
}

restart_marzban_runtime() {
  local code runtime_ready=0
  if ! docker compose -f "$compose_file" restart marzban >/dev/null; then
    echo "could not restart Marzban/Xray runtime" >&2
    return 1
  fi
  for _ in {1..60}; do
    code="$(
      api_request GET "/api/users?offset=0&limit=1" \
        "$work_dir/runtime-readiness.json" 2>/dev/null || true
    )"
    if [[ "$code" == "200" ]] &&
      jq -e '(.users | type == "array") and (.total | type == "number")' \
        "$work_dir/runtime-readiness.json" >/dev/null 2>&1; then
      runtime_ready=1
      break
    fi
    sleep 1
  done
  if (( ! runtime_ready )); then
    echo "Marzban API did not recover after runtime restart" >&2
    return 1
  fi
}

verify_restored_runtime_users() {
  local index=0 username identifier encoded code
  local active_users="$work_dir/restored-active-users.json"
  if ! isolation_state_is_valid ||
    ! jq -e '.status == "restored"' "$isolation_state_file" >/dev/null ||
    ! systemctl is-active --quiet "$worker_service" ||
    ! worker_reconciliation_is_fresh; then
    echo "restored XHTTP users or worker are not ready" >&2
    return 1
  fi
  while IFS=$'\t' read -r username identifier; do
    index="$((index + 1))"
    encoded="$(encoded_username "$username")"
    code="$(
      api_request GET "/api/user/$encoded" \
        "$work_dir/restored-user-${index}.json"
    )"
    if [[ "$code" == "404" ]]; then
      continue
    fi
    if [[ "$code" != "200" ]] ||
      ! jq -e \
        --arg uuid "$identifier" \
        --arg tag "$canary_inbound_tag" \
        --argjson now "$(date -u +%s)" '
          .proxies.vless.id == $uuid and
          (.inbounds.vless | type == "array") and
          (.inbounds.vless | index($tag)) != null and
          (
            ((.expire | type) != "number") or
            .expire == 0 or
            .expire > $now or
            .status != "active"
          )
        ' "$work_dir/restored-user-${index}.json" >/dev/null; then
      echo "restored XHTTP runtime identity/status verification failed" >&2
      return 1
    fi
  done < <(
    jq -r '.users[] | [.username, .uuid] | @tsv' "$isolation_state_file"
  )
  if ! list_active_xhttp_users "$active_users" ||
    ! jq -e '
      all(.[]; (.username | startswith("cea_canary_") | not))
    ' "$active_users" >/dev/null; then
    echo "canary identity survived production runtime restoration" >&2
    return 1
  fi
}

finalize_restoration() {
  if [[ ! -e "$isolation_state_file" ]]; then
    return 0
  fi
  if ! verify_restored_runtime_users; then
    return 1
  fi
  rm -f -- "$isolation_state_file"
  [[ ! -e "$isolation_state_file" ]]
}

complete_restoration_behind_closed_ingress() {
  if [[ ! -e "$isolation_state_file" ]]; then
    return 0
  fi
  if ! public_ingress_is_closed ||
    [[ ! -f "$ingress_closed_marker" ]] ||
    ! restart_marzban_runtime ||
    ! finalize_restoration; then
    echo "XHTTP restoration/runtime verification failed; ingress stays closed" >&2
    return 1
  fi
}

verify_canary_isolation() {
  local active_users="$work_dir/isolation-active-users.json"
  local identity username identifier
  if ! isolation_state_is_valid ||
    ! jq -e '.status == "isolated"' "$isolation_state_file" >/dev/null ||
    systemctl is-active --quiet "$worker_service"; then
    echo "canary isolation or worker stop is not effective" >&2
    return 1
  fi
  identity="$(canary_identity)"
  username="${identity%%$'\n'*}"
  identifier="${identity#*$'\n'}"
  if ! list_active_xhttp_users "$active_users"; then
    return 1
  fi
  jq -e \
    --arg username "$username" \
    --arg uuid "$identifier" '
      length == 1 and
      .[0].username == $username and
      .[0].uuid == $uuid
    ' "$active_users" >/dev/null
}

canary_identity() {
  validate_canary_state active || {
    echo "invalid or missing active canary state" >&2
    return 1
  }
  jq -er '.username + "\n" + .uuid' "$canary_state_file"
}

write_canary_audit() {
  local status="$1"
  local username="$2"
  local output
  output="$(mktemp "${canary_state_file}.new.XXXXXX")"
  if ! CEAVPN_CANARY_STATUS="$status" \
    CEAVPN_CANARY_USERNAME="$username" \
    CEAVPN_CANARY_STATE_OUTPUT="$output" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

status = os.environ["CEAVPN_CANARY_STATUS"]
if status not in {"revoked", "consumed"}:
    raise SystemExit("invalid canary audit status")
payload = {
    "status": status,
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "username": os.environ["CEAVPN_CANARY_USERNAME"],
}
Path(os.environ["CEAVPN_CANARY_STATE_OUTPUT"]).write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  then
    rm -f -- "$output"
    return 1
  fi
  if ! chmod 0600 "$output" ||
    ! mv "$output" "$canary_state_file" ||
    ! rm -f -- "$canary_uri_file"; then
    rm -f -- "$output"
    return 1
  fi
}

delete_canary() {
  local audit_status="$1"
  local identity username delete_code
  if ! identity="$(canary_identity)"; then
    return 1
  fi
  username="${identity%%$'\n'*}"
  delete_code="$(
    api_request DELETE "/api/user/$username" \
      "$work_dir/canary-delete-response.json"
  )"
  if [[ "$delete_code" != "200" && "$delete_code" != "204" &&
    "$delete_code" != "404" ]]; then
    echo "could not delete whitelist canary account" >&2
    return 1
  fi
  write_canary_audit "$audit_status" "$username"
}

create_canary() {
  local state_status identity username expected_uuid uuid expire payload create_code
  local current_code delete_code
  if [[ -s "$canary_state_file" ]]; then
    state_status="$(jq -er '.status // empty' "$canary_state_file" 2>/dev/null || true)"
    if [[ "$state_status" == "active" ]]; then
      if ! validate_canary_state active; then
        echo "invalid active canary state; refusing replacement" >&2
        return 1
      fi
      identity="$(canary_identity)"
      username="${identity%%$'\n'*}"
      expected_uuid="${identity#*$'\n'}"
      current_code="$(
        api_request GET "/api/user/$username" "$work_dir/canary-current.json"
      )"
      if [[ "$current_code" == "200" ]] &&
        jq -e \
          --arg uuid "$expected_uuid" \
          --arg tag "$canary_inbound_tag" \
          --argjson data_limit "$canary_data_limit" \
          --argjson now "$(date -u +%s)" '
          .status == "active" and
          .proxies.vless.id == $uuid and
          .proxies.vless.flow == "" and
          .inbounds.vless == [$tag] and
          .data_limit == $data_limit and
          (.expire | type == "number" and . > $now)
        ' "$work_dir/canary-current.json" >/dev/null &&
        [[ -s "$canary_uri_file" ]]; then
        echo "whitelist canary is already active"
        echo "root-only URI file: $canary_uri_file"
        return 0
      fi
      if [[ "$current_code" != "200" && "$current_code" != "404" ]]; then
        echo "could not inspect existing whitelist canary" >&2
        return 1
      fi
      if [[ "$current_code" == "200" ]]; then
        delete_code="$(
          api_request DELETE "/api/user/$username" \
            "$work_dir/canary-replace-response.json"
        )"
        if [[ "$delete_code" != "200" && "$delete_code" != "204" &&
          "$delete_code" != "404" ]]; then
          echo "could not remove expired whitelist canary" >&2
          return 1
        fi
      fi
      write_canary_audit "revoked" "$username"
    elif [[ "$state_status" == "revoked" || "$state_status" == "consumed" ]]; then
      if ! validate_canary_state any-audit; then
        echo "invalid canary audit state; refusing replacement" >&2
        return 1
      fi
    else
      echo "invalid canary state; refusing replacement" >&2
      return 1
    fi
  fi

  uuid="$(cat /proc/sys/kernel/random/uuid)"
  username="cea_canary_$(tr -d '-' </proc/sys/kernel/random/uuid | cut -c1-12)"
  expire="$(( $(date -u +%s) + canary_lifetime_seconds ))"
  payload="$work_dir/canary-create.json"
  jq -n \
    --arg username "$username" \
    --arg uuid "$uuid" \
    --arg tag "$canary_inbound_tag" \
    --argjson expire "$expire" \
    --argjson data_limit "$canary_data_limit" \
    '{
      username: $username,
      proxies: {vless: {id: $uuid, flow: ""}},
      inbounds: {vless: [$tag]},
      expire: $expire,
      data_limit: $data_limit,
      data_limit_reset_strategy: "no_reset",
      status: "active",
      note: "CEA VPN restricted-SIM whitelist canary"
    }' >"$payload"

  create_code="$(
    api_request POST /api/user "$work_dir/canary-create-response.json" "$payload"
  )"
  if [[ "$create_code" != "200" && "$create_code" != "201" ]]; then
    echo "could not create whitelist canary account" >&2
    return 1
  fi
  rollback_canary_username="$username"
  if ! jq -e \
    --arg username "$username" \
    --arg uuid "$uuid" \
    --arg tag "$canary_inbound_tag" \
    --argjson expire "$expire" \
    --argjson data_limit "$canary_data_limit" '
      .username == $username and
      .status == "active" and
      .proxies.vless.id == $uuid and
      .proxies.vless.flow == "" and
      .inbounds.vless == [$tag] and
      .expire == $expire and
      .data_limit == $data_limit
    ' "$work_dir/canary-create-response.json" >/dev/null; then
    echo "whitelist canary verification failed" >&2
    return 1
  fi

  canary_state_tmp="$(mktemp "${canary_state_file}.new.XXXXXX")"
  canary_uri_tmp="$(mktemp "${canary_uri_file}.new.XXXXXX")"
  CEAVPN_CANARY_USERNAME="$username" \
  CEAVPN_CANARY_UUID="$uuid" \
  CEAVPN_CANARY_EXPIRE="$expire" \
  CEAVPN_CANARY_DATA_LIMIT="$canary_data_limit" \
  CEAVPN_CANARY_ADDRESS="$CEAVPN_PUBLIC_IP" \
  CEAVPN_CANARY_SNI="$CEAVPN_COVER_DOMAIN" \
  CEAVPN_CANARY_PUBLIC_KEY="$REALITY_PUBLIC_KEY" \
  CEAVPN_CANARY_SHORT_ID="$REALITY_SHORT_ID" \
  CEAVPN_CANARY_PATH="$XHTTP_PATH" \
  CEAVPN_CANARY_STATE_OUTPUT="$canary_state_tmp" \
  CEAVPN_CANARY_URI_OUTPUT="$canary_uri_tmp" \
    python3 - <<'PY'
import ipaddress
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

identifier = str(uuid.UUID(os.environ["CEAVPN_CANARY_UUID"]))
address = str(ipaddress.ip_address(os.environ["CEAVPN_CANARY_ADDRESS"]))
authority_address = f"[{address}]" if ":" in address else address
xhttp_extra = {
    "scMaxEachPostBytes": 1000000,
    "scMaxConcurrentPosts": 100,
    "scMinPostsIntervalMs": 30,
    "xPaddingBytes": "100-1000",
    "noGRPCHeader": False,
}
query = urlencode(
    {
        "encryption": "none",
        "security": "reality",
        "sni": os.environ["CEAVPN_CANARY_SNI"],
        "fp": "chrome",
        "pbk": os.environ["CEAVPN_CANARY_PUBLIC_KEY"],
        "sid": os.environ["CEAVPN_CANARY_SHORT_ID"],
        "type": "xhttp",
        "headerType": "",
        "path": os.environ["CEAVPN_CANARY_PATH"],
        "mode": "auto",
        "extra": json.dumps(
            xhttp_extra,
            sort_keys=True,
            separators=(",", ":"),
        ),
    },
    quote_via=quote,
    safe="",
)
uri = (
    f"vless://{identifier}@{authority_address}:443?{query}"
    f"#CEA%20VPN%20whitelist%20canary"
)
Path(os.environ["CEAVPN_CANARY_URI_OUTPUT"]).write_text(
    uri + "\n", encoding="utf-8"
)
payload = {
    "status": "active",
    "username": os.environ["CEAVPN_CANARY_USERNAME"],
    "uuid": identifier,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "expires_at": int(os.environ["CEAVPN_CANARY_EXPIRE"]),
    "data_limit": int(os.environ["CEAVPN_CANARY_DATA_LIMIT"]),
}
Path(os.environ["CEAVPN_CANARY_STATE_OUTPUT"]).write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  chmod 0600 "$canary_state_tmp" "$canary_uri_tmp"
  mv "$canary_uri_tmp" "$canary_uri_file"
  canary_uri_tmp=""
  mv "$canary_state_tmp" "$canary_state_file"
  canary_state_tmp=""
  rollback_canary_username=""
  echo "whitelist canary created for 45 minutes with a 100-MiB cap"
  echo "root-only URI file: $canary_uri_file"
  echo "the URI was not printed; transfer it only over an already trusted channel"
}

canary_status() {
  local state_status identity username code
  if [[ ! -s "$canary_state_file" ]]; then
    echo "absent"
    return 0
  fi
  state_status="$(jq -er '.status // empty' "$canary_state_file" 2>/dev/null || true)"
  if [[ "$state_status" == "revoked" || "$state_status" == "consumed" ]]; then
    if ! validate_canary_state any-audit; then
      echo "invalid canary audit state" >&2
      return 1
    fi
    echo "$state_status"
    return 0
  fi
  identity="$(canary_identity)"
  username="${identity%%$'\n'*}"
  code="$(api_request GET "/api/user/$username" "$work_dir/canary-status.json")"
  if [[ "$code" == "404" ]]; then
    echo "missing"
    return 1
  fi
  if [[ "$code" != "200" ]]; then
    echo "could not inspect whitelist canary" >&2
    return 1
  fi
  jq -r '
    "status=\(.status)",
    "expires_at=\(.expire)",
    "used_traffic=\(.used_traffic)",
    "online_at=\(.online_at // "")"
  ' "$work_dir/canary-status.json"
  echo "uri_file=$canary_uri_file"
}

verify_canary_evidence() {
  local identity username expected_uuid code
  identity="$(canary_identity)"
  username="${identity%%$'\n'*}"
  expected_uuid="${identity#*$'\n'}"
  code="$(api_request GET "/api/user/$username" "$work_dir/canary-evidence.json")"
  if [[ "$code" != "200" ]]; then
    echo "could not inspect active whitelist canary" >&2
    return 1
  fi
  CEAVPN_CANARY_RESPONSE="$work_dir/canary-evidence.json" \
  CEAVPN_CANARY_EXPECTED_UUID="$expected_uuid" \
  CEAVPN_CANARY_EXPECTED_TAG="$canary_inbound_tag" \
  CEAVPN_CANARY_MINIMUM_USAGE="$canary_minimum_usage" \
  CEAVPN_CANARY_DATA_LIMIT="$canary_data_limit" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = json.loads(
    Path(os.environ["CEAVPN_CANARY_RESPONSE"]).read_text(encoding="utf-8")
)
now = datetime.now(timezone.utc)
online_at = payload.get("online_at")
if not isinstance(online_at, str):
    raise SystemExit("canary has no online activity")
try:
    online = datetime.fromisoformat(online_at.replace("Z", "+00:00"))
except ValueError as exc:
    raise SystemExit("invalid canary online timestamp") from exc
if online.tzinfo is None:
    online = online.replace(tzinfo=timezone.utc)
if not 0 <= (now - online.astimezone(timezone.utc)).total_seconds() <= 900:
    raise SystemExit("canary activity is not recent")
minimum_usage = int(os.environ["CEAVPN_CANARY_MINIMUM_USAGE"])
if (
    payload.get("status") != "active"
    or payload.get("proxies", {}).get("vless", {}).get("id")
    != os.environ["CEAVPN_CANARY_EXPECTED_UUID"]
    or payload.get("proxies", {}).get("vless", {}).get("flow") != ""
    or payload.get("inbounds", {}).get("vless")
    != [os.environ["CEAVPN_CANARY_EXPECTED_TAG"]]
    or payload.get("data_limit") != int(os.environ["CEAVPN_CANARY_DATA_LIMIT"])
    or not isinstance(payload.get("used_traffic"), int)
    or payload["used_traffic"] <= minimum_usage
    or not isinstance(payload.get("expire"), int)
    or payload["expire"] <= int(now.timestamp())
):
    raise SystemExit("canary has not met the full-tunnel evidence gate")
PY
}

write_state() {
  local state="$1"
  local operator="$2"
  local region="$3"
  local state_tmp
  state_tmp="$(mktemp "${qualification_file}.new.XXXXXX")"
  trap 'rm -f -- "${state_tmp:-}"' RETURN
  CEAVPN_QUALIFICATION_STATE="$state" \
  CEAVPN_QUALIFICATION_OPERATOR="$operator" \
  CEAVPN_QUALIFICATION_REGION="$region" \
  CEAVPN_QUALIFICATION_PROBE_URL="$probe_url" \
  CEAVPN_QUALIFICATION_FINGERPRINT="$qualification_fingerprint" \
  CEAVPN_QUALIFICATION_OUTPUT="$state_tmp" \
    python3 - <<'PY'
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

state = os.environ["CEAVPN_QUALIFICATION_STATE"]
if state not in {"passed", "revoked"}:
    raise SystemExit("invalid qualification state")
recorded_at = datetime.now(timezone.utc)
fingerprint = os.environ["CEAVPN_QUALIFICATION_FINGERPRINT"]
if state == "passed" and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
    raise SystemExit("missing qualification config fingerprint")
payload = {
    "status": state,
    "recorded_at": recorded_at.isoformat(),
    "valid_until": (
        (recorded_at + timedelta(hours=24)).isoformat()
        if state == "passed"
        else recorded_at.isoformat()
    ),
    "operator": os.environ["CEAVPN_QUALIFICATION_OPERATOR"],
    "region": os.environ["CEAVPN_QUALIFICATION_REGION"],
    "probe_url": os.environ["CEAVPN_QUALIFICATION_PROBE_URL"],
    "config_fingerprint": fingerprint if state == "passed" else "",
    "evidence": (
        "restricted-sim-xhttp-tunnel-worked"
        if state == "passed"
        else "operator-revoked"
    ),
    "checks": (
        [
            "xhttp_profile_connected",
            "dns_through_tunnel",
            "telegram_through_tunnel",
            "https_through_tunnel",
            "transfer_over_1mib",
        ]
        if state == "passed"
        else []
    ),
}
Path(os.environ["CEAVPN_QUALIFICATION_OUTPUT"]).write_text(
    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  chmod 0600 "$state_tmp"
  mv "$state_tmp" "$qualification_file"
  trap - RETURN
}

configure_publication_gate() {
  if [[ ! -x /opt/ceavpn/configure-whitelist-host.sh ]]; then
    echo "missing whitelist publication gate helper" >&2
    return 1
  fi
  /opt/ceavpn/configure-whitelist-host.sh
}

command="${1:-}"
case "$command" in
  boot-firewall-close)
    boot_failed=0
    if ! close_public_firewall_at_boot; then
      boot_failed=1
    fi
    if ! remove_public_status; then
      boot_failed=1
    fi
    if (( boot_failed )); then
      echo "boot-time whitelist firewall close failed" >&2
      exit 1
    fi
    ;;
  probe)
    printf '%s\n' "$probe_url"
    echo "Open this URL from the affected mobile SIM with VPN disabled."
    echo "This checks candidate reachability only and does not qualify the tunnel."
    echo "Afterward import the XHTTP profile and test DNS, Telegram, external HTTPS,"
    echo "and more than 1 MiB of traffic through VPN before running pass."
    ;;
  status)
    if [[ ! -s "$qualification_file" ]]; then
      echo "pending"
      exit 0
    fi
    if ! load_gate_context; then
      echo "inactive"
      echo "reason=gate-context-unavailable"
      exit 1
    fi
    if ! qualification_fingerprint="$(compute_config_fingerprint)"; then
      echo "inactive"
      echo "reason=config-fingerprint-unavailable"
      exit 1
    fi
    effective_current="0"
    if qualification_record_is_current "$qualification_fingerprint"; then
      effective_current="1"
    fi
    CEAVPN_QUALIFICATION_FILE="$qualification_file" \
    CEAVPN_QUALIFICATION_EFFECTIVE_CURRENT="$effective_current" \
      python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["CEAVPN_QUALIFICATION_FILE"]).read_text(encoding="utf-8")
)
allowed = {
    "status",
    "recorded_at",
    "operator",
    "region",
    "probe_url",
    "config_fingerprint",
    "valid_until",
    "evidence",
    "checks",
}
if not isinstance(payload, dict) or set(payload) != allowed:
    raise SystemExit("invalid qualification record")
for key in allowed - {"checks"}:
    if not isinstance(payload[key], str):
        raise SystemExit("invalid qualification record")
if not isinstance(payload["checks"], list) or not all(
    isinstance(item, str) for item in payload["checks"]
):
    raise SystemExit("invalid qualification record")
raw_status = payload["status"]
effective_status = (
    "passed"
    if os.environ["CEAVPN_QUALIFICATION_EFFECTIVE_CURRENT"] == "1"
    else ("revoked" if raw_status == "revoked" else "inactive")
)
print(effective_status)
print(f"recorded_status={raw_status}")
print(f"recorded_at={payload['recorded_at']}")
print(f"operator={payload['operator']}")
print(f"region={payload['region']}")
print(f"probe_url={payload['probe_url']}")
print(f"valid_until={payload['valid_until']}")
print(f"config_fingerprint={payload['config_fingerprint']}")
print(f"evidence={payload['evidence']}")
print(f"checks={','.join(payload['checks'])}")
PY
    ;;
  canary-create)
    if ! load_gate_context; then
      force_gate_closed || true
      exit 1
    fi
    if ! qualification_fingerprint="$(compute_config_fingerprint)"; then
      force_gate_closed || true
      echo "could not fingerprint the canary configuration" >&2
      exit 1
    fi
    if qualification_record_is_current "$qualification_fingerprint"; then
      echo "revoke the current qualification before creating a new canary" >&2
      exit 1
    fi
    if ! force_gate_closed; then
      echo "could not establish a closed baseline for the canary" >&2
      exit 1
    fi
    if ! configure_publication_gate; then
      force_gate_closed || true
      echo "could not disable normal whitelist profiles before canary" >&2
      exit 1
    fi
    if ! local_probe ||
      ! xray_inbound_is_healthy ||
      ! remote_cover_is_healthy ||
      ! relay_e2e_is_healthy; then
      echo "local HTTPS, Xray inbound, or foreign relay check failed; canary was not created" >&2
      exit 1
    fi
    if ! prepare_canary_api; then
      force_gate_closed || true
      echo "canary API unavailable; ingress remains closed" >&2
      exit 1
    fi
    if ! restore_isolated_users; then
      force_gate_closed || true
      echo "could not recover a previous canary isolation" >&2
      exit 1
    fi
    if ! systemctl is-active --quiet "$worker_service" ||
      ! worker_reconciliation_is_fresh; then
      force_gate_closed || true
      echo "VPN worker must be active and reconciled before canary isolation" >&2
      exit 1
    fi
    if ! isolate_non_canary_users; then
      if force_gate_closed; then
        if restore_isolated_users; then
          complete_restoration_behind_closed_ingress || true
        fi
      fi
      echo "could not isolate ordinary XHTTP users for canary" >&2
      exit 1
    fi
    if ! create_canary; then
      if force_gate_closed; then
        if restore_isolated_users; then
          complete_restoration_behind_closed_ingress || true
        fi
      fi
      exit 1
    fi
    if ! restart_marzban_runtime ||
      ! verify_canary_isolation ||
      ! xray_inbound_is_healthy ||
      ! remote_cover_is_healthy ||
      ! relay_e2e_is_healthy; then
      delete_canary "revoked" || true
      if force_gate_closed; then
        if restore_isolated_users; then
          complete_restoration_behind_closed_ingress || true
        fi
      fi
      echo "canary is not the only active XHTTP account" >&2
      exit 1
    fi
    if ! open_public_ingress; then
      echo "could not open candidate port 443 for canary" >&2
      delete_canary "revoked" || true
      if force_gate_closed; then
        if restore_isolated_users; then
          complete_restoration_behind_closed_ingress || true
        fi
      fi
      exit 1
    fi
    ;;
  canary-status)
    if ! prepare_canary_api; then
      echo "canary API unavailable; bounded canary remains unchanged" >&2
      exit 1
    fi
    canary_status
    ;;
  canary-revoke)
    canary_cleanup_username=""
    if ! force_gate_closed; then
      echo "canary revoke hard-stop failed; users remain isolated" >&2
      exit 1
    fi
    if [[ ! -s "$canary_state_file" ]]; then
      if [[ ! -e "$isolation_state_file" ]]; then
        echo "absent"
        exit 0
      fi
      if ! prepare_canary_api; then
        echo "canary API unavailable; ingress remains hard-stopped" >&2
        exit 1
      fi
      if ! restore_isolated_users; then
        echo "could not restore isolated users behind closed ingress" >&2
        exit 1
      fi
      if ! complete_restoration_behind_closed_ingress; then
        exit 1
      fi
      echo "absent"
      exit 0
    fi
    state_status="$(
      jq -er '.status // empty' "$canary_state_file" 2>/dev/null || true
    )"
    if [[ "$state_status" == "active" ]]; then
      if ! validate_canary_state active; then
        force_gate_closed || true
        rm -f -- "$canary_uri_file"
        echo "invalid active canary state; ingress remains hard-stopped" >&2
        exit 1
      fi
      canary_cleanup_username="$(jq -er '.username' "$canary_state_file")"
      # Tombstone first. A Marzban outage after this point cannot let the
      # periodic enforcer reopen the bounded canary window.
      if ! write_canary_audit "revoked" "$canary_cleanup_username"; then
        force_gate_closed || true
        echo "could not persist local canary revocation" >&2
        exit 1
      fi
    elif [[ "$state_status" == "revoked" ||
      "$state_status" == "consumed" ]]; then
      if ! validate_canary_state any-audit; then
        force_gate_closed || true
        rm -f -- "$canary_uri_file"
        echo "invalid canary audit state; ingress remains hard-stopped" >&2
        exit 1
      fi
      canary_cleanup_username="$(jq -er '.username' "$canary_state_file")"
      rm -f -- "$canary_uri_file"
    else
      force_gate_closed || true
      rm -f -- "$canary_uri_file"
      echo "invalid canary state; ingress remains hard-stopped" >&2
      exit 1
    fi
    if ! prepare_canary_api; then
      echo "canary revoked locally; API cleanup will be retried while ingress stays closed" >&2
      exit 1
    fi
    canary_delete_code="$(
      api_request DELETE "/api/user/$canary_cleanup_username" \
        "$work_dir/canary-revoke-response.json"
    )"
    if [[ "$canary_delete_code" != "200" &&
      "$canary_delete_code" != "204" &&
      "$canary_delete_code" != "404" ]]; then
      echo "could not clean up the revoked canary account" >&2
      exit 1
    fi
    if ! restore_isolated_users; then
      echo "could not restore isolated users behind closed ingress" >&2
      exit 1
    fi
    if ! complete_restoration_behind_closed_ingress; then
      exit 1
    fi
    if ! local_probe ||
      ! xray_inbound_is_healthy ||
      ! remote_cover_is_healthy ||
      ! relay_e2e_is_healthy; then
      force_gate_closed || true
      echo "runtime/foreign relay health failed after canary cleanup" >&2
      exit 1
    fi
    echo "whitelist canary revoked and local URI removed"
    ;;
  pass)
    shift
    operator=""
    region=""
    confirmation=""
    while (($#)); do
      case "$1" in
        --operator)
          [[ $# -ge 2 ]] || { usage >&2; exit 2; }
          operator="$2"
          shift 2
          ;;
        --region)
          [[ $# -ge 2 ]] || { usage >&2; exit 2; }
          region="$2"
          shift 2
          ;;
        --confirm)
          [[ $# -ge 2 ]] || { usage >&2; exit 2; }
          confirmation="$2"
          shift 2
          ;;
        *)
          usage >&2
          exit 2
          ;;
      esac
    done
    if ! validate_label "$operator" || ! validate_label "$region"; then
      echo "operator and region must be short labels" >&2
      exit 2
    fi
    if [[ "$confirmation" != "$confirmation_phrase" ]]; then
      echo "explicit full-tunnel restricted-SIM confirmation is required" >&2
      exit 2
    fi
    if ! load_gate_context ||
      ! local_probe ||
      ! xray_inbound_is_healthy ||
      ! remote_cover_is_healthy; then
      force_gate_closed || true
      echo "local HTTPS or Xray inbound check failed; publication remains disabled" >&2
      exit 1
    fi
    if ! prepare_canary_api; then
      echo "canary API unavailable; bounded canary remains unchanged" >&2
      exit 1
    fi
    if ! verify_canary_evidence; then
      echo "canary must be active, recently online, and over 1 MiB before pass" >&2
      exit 1
    fi
    if ! qualification_fingerprint="$(compute_config_fingerprint)"; then
      echo "could not fingerprint the qualified configuration" >&2
      force_gate_closed || true
      exit 1
    fi
    if ! force_gate_closed; then
      echo "could not hard-stop ingress before promoting canary" >&2
      exit 1
    fi
    delete_failed=0
    if ! delete_canary "consumed"; then
      delete_failed=1
    fi
    if ! restore_isolated_users; then
      echo "could not restore isolated users; ingress remains closed" >&2
      exit 1
    fi
    if ! complete_restoration_behind_closed_ingress; then
      exit 1
    fi
    if (( delete_failed )); then
      echo "canary cleanup failed; ingress remains closed" >&2
      exit 1
    fi
    if ! local_probe ||
      ! xray_inbound_is_healthy ||
      ! remote_cover_is_healthy ||
      ! relay_e2e_is_healthy; then
      force_gate_closed || true
      echo "restored production runtime/relay health failed; ingress remains closed" >&2
      exit 1
    fi
    write_state "passed" "$operator" "$region"
    if ! configure_publication_gate; then
      echo "whitelist publication gate could not be opened" >&2
      force_gate_closed || true
      qualification_fingerprint=""
      write_state "revoked" "$operator" "$region"
      configure_publication_gate || true
      exit 1
    fi
    if ! public_profile_fingerprint="$(compute_public_profile_fingerprint)"; then
      echo "public profile fingerprint failed; closing gate" >&2
      force_gate_closed || true
      qualification_fingerprint=""
      write_state "revoked" "$operator" "$region"
      configure_publication_gate || true
      exit 1
    fi
    if ! publish_public_status \
      "$qualification_fingerprint" "$public_profile_fingerprint"; then
      echo "public qualification status could not be published; closing gate" >&2
      force_gate_closed || true
      qualification_fingerprint=""
      write_state "revoked" "$operator" "$region"
      configure_publication_gate || true
      exit 1
    fi
    if ! open_public_ingress; then
      echo "public ingress could not be opened; closing qualification gate" >&2
      force_gate_closed || true
      qualification_fingerprint=""
      write_state "revoked" "$operator" "$region"
      configure_publication_gate || true
      exit 1
    fi
    echo "whitelist XHTTP tunnel qualified; publication gate is open"
    ;;
  revoke)
    close_failed=0
    canary_cleanup_username=""
    if ! force_gate_closed; then
      close_failed=1
    fi
    if [[ -s "$canary_state_file" ]]; then
      state_status="$(
        jq -er '.status // empty' "$canary_state_file" 2>/dev/null || true
      )"
      if [[ "$state_status" == "active" ]]; then
        if validate_canary_state active; then
          canary_cleanup_username="$(
            jq -er '.username' "$canary_state_file"
          )"
          # Persist the local revocation before any API call. Even when
          # Marzban is unavailable, enforce can no longer reopen this canary.
          if ! write_canary_audit \
            "revoked" "$canary_cleanup_username"; then
            close_failed=1
          fi
        else
          rm -f -- "$canary_uri_file"
          close_failed=1
        fi
      elif [[ "$state_status" == "revoked" ||
        "$state_status" == "consumed" ]]; then
        if validate_canary_state any-audit; then
          canary_cleanup_username="$(
            jq -er '.username' "$canary_state_file"
          )"
          rm -f -- "$canary_uri_file"
        else
          rm -f -- "$canary_uri_file"
          close_failed=1
        fi
      else
        rm -f -- "$canary_uri_file"
        close_failed=1
      fi
    fi
    operator=""
    region=""
    if [[ -s "$qualification_file" ]]; then
      readarray -t labels < <(
        CEAVPN_QUALIFICATION_FILE="$qualification_file" python3 - <<'PY'
import json
import os
from pathlib import Path

try:
    payload = json.loads(
        Path(os.environ["CEAVPN_QUALIFICATION_FILE"]).read_text(encoding="utf-8")
    )
except (OSError, ValueError):
    payload = {}
print(str(payload.get("operator") or ""))
print(str(payload.get("region") or ""))
PY
      )
      operator="${labels[0]:-}"
      region="${labels[1]:-}"
    fi
    qualification_fingerprint=""
    if ! write_state "revoked" "$operator" "$region"; then
      close_failed=1
    fi
    if ! configure_publication_gate; then
      close_failed=1
    fi
    if [[ -n "$canary_cleanup_username" ||
      -e "$isolation_state_file" ]]; then
      if ! prepare_canary_api; then
        close_failed=1
      else
        canary_cleanup_failed=0
        if [[ -n "$canary_cleanup_username" ]]; then
          canary_delete_code="$(
            api_request DELETE "/api/user/$canary_cleanup_username" \
              "$work_dir/emergency-canary-delete.json"
          )"
          if [[ "$canary_delete_code" != "200" &&
            "$canary_delete_code" != "204" &&
            "$canary_delete_code" != "404" ]]; then
            canary_cleanup_failed=1
            close_failed=1
          fi
        fi
        if (( ! canary_cleanup_failed )) &&
          [[ -e "$isolation_state_file" ]]; then
          if ! restore_isolated_users ||
            ! complete_restoration_behind_closed_ingress; then
            close_failed=1
          fi
        fi
      fi
    fi
    if (( close_failed )); then
      echo "qualification/canary revoked locally; cleanup needs retry while ingress stays closed" >&2
      exit 1
    fi
    echo "whitelist qualification revoked; publication gate is closed"
    ;;
  enforce)
    if ! prepare_canary_api; then
      force_gate_closed || true
      echo "whitelist gate context is unavailable; public ingress closed" >&2
      exit 1
    fi
    if ! qualification_fingerprint="$(compute_config_fingerprint)"; then
      force_gate_closed || true
      echo "whitelist config fingerprint failed; public ingress closed" >&2
      exit 1
    fi
    if [[ -s "$canary_state_file" ]] &&
      jq -e '.status == "revoked"' "$canary_state_file" >/dev/null 2>&1; then
      force_gate_closed || true
      configure_publication_gate || true
      echo "revoked canary tombstone blocks ingress until cleanup/new canary" >&2
      exit 1
    fi
    if qualification_record_is_current "$qualification_fingerprint"; then
      if [[ -e "$isolation_state_file" ]]; then
        if ! force_gate_closed ||
          ! restore_isolated_users ||
          ! complete_restoration_behind_closed_ingress; then
          force_gate_closed || true
          echo "leftover canary isolation could not be recovered" >&2
          exit 1
        fi
      fi
      if ! systemctl is-active --quiet "$worker_service" ||
        ! worker_reconciliation_is_fresh ||
        ! local_probe ||
        ! xray_inbound_is_healthy ||
        ! remote_cover_is_healthy ||
        ! relay_e2e_is_healthy; then
        force_gate_closed || true
        echo "qualified whitelist runtime/relay health failed" >&2
        exit 1
      fi
      if ! configure_publication_gate; then
        force_gate_closed || true
        echo "whitelist host gate enforcement failed; public ingress closed" >&2
        exit 1
      fi
      if ! public_profile_fingerprint="$(compute_public_profile_fingerprint)"; then
        force_gate_closed || true
        echo "public profile fingerprint failed; public ingress closed" >&2
        exit 1
      fi
      if ! publish_public_status \
        "$qualification_fingerprint" "$public_profile_fingerprint"; then
        force_gate_closed || true
        echo "whitelist status publication failed; public ingress closed" >&2
        exit 1
      fi
      if ! open_public_ingress; then
        force_gate_closed || true
        echo "whitelist ingress failed to open and was closed again" >&2
        exit 1
      fi
      echo "whitelist qualification is current; public ingress remains open"
      exit 0
    fi

    if canary_window_is_active; then
      if ! force_gate_closed; then
        echo "canary hard-stop verification failed" >&2
        exit 1
      fi
      if ! configure_publication_gate; then
        force_gate_closed || true
        echo "whitelist host disable failed; public ingress closed" >&2
        exit 1
      fi
      if ! isolate_non_canary_users ||
        ! restart_marzban_runtime ||
        ! verify_canary_isolation ||
        ! xray_inbound_is_healthy ||
        ! remote_cover_is_healthy ||
        ! relay_e2e_is_healthy; then
        force_gate_closed || true
        echo "bounded canary isolation verification failed" >&2
        exit 1
      fi
      if ! open_public_ingress; then
        force_gate_closed || true
        echo "canary ingress failed to open and was closed again" >&2
        exit 1
      fi
      echo "qualification is not current; only the bounded canary window remains open"
    else
      close_failed=0
      if ! force_gate_closed; then
        close_failed=1
      fi
      if [[ -e "$isolation_state_file" ]]; then
        if ! restore_isolated_users ||
          ! complete_restoration_behind_closed_ingress; then
          close_failed=1
        fi
      fi
      if ! configure_publication_gate; then
        echo "whitelist host disable failed; public ingress closed" >&2
        exit 1
      fi
      if (( close_failed )); then
        echo "qualification is not current; hard-stop verification failed" >&2
        exit 1
      fi
      echo "qualification is not current; public ingress closed"
    fi
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
