#!/usr/bin/env bash
set -Eeuo pipefail

# Run this helper on an existing foreign CEA VPN exit. It creates one
# non-customer Marzban account restricted to the private WS/TLS fallback
# inbound, then copies a root-only exit environment file to the candidate.

admin_file="/root/ceavpn-admin.env"
fallback_file="/root/ceavpn-fallback.env"
node_file="/root/ceavpn-node.env"
state_dir="/root/ceavpn-whitelist-relays"
api_base="http://127.0.0.1:8000"
fallback_tag="VLESS WS TLS FALLBACK"
work_dir=""
rollback_mode=""
username=""
token=""
remote_rollback_mode=""
remote_tmp=""
remote_backup=""
remote_absent_marker=""
remote_revoked=""

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

usage() {
  cat <<'EOF'
Usage:
  provision-whitelist-relay.sh create --gateway-id ID --candidate root@HOST
  provision-whitelist-relay.sh status --gateway-id ID
  provision-whitelist-relay.sh revoke --gateway-id ID [--candidate root@HOST]

Use a short-lived root SSH key and remove it immediately afterward. The
candidate host key must already be trusted. Secrets are written only to
root-only files and are never printed.
EOF
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
  jq -er '.access_token'
}

authorized_request() {
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

remote_root_sh() {
  local args=(ssh -o BatchMode=yes "$candidate" sh -s --)
  "${args[@]}" "$@"
}

rollback() {
  set +e
  if [[ "$remote_rollback_mode" == "install" && -n "$candidate" ]]; then
    remote_root_sh "$remote_tmp" "$remote_backup" "$remote_absent_marker" <<'SH'
set -eu
remote_tmp=$1
remote_backup=$2
remote_absent_marker=$3
target=/root/ceavpn-lte-exit.env
if [ -e "$remote_backup" ]; then
  mv "$remote_backup" "$target"
elif [ -e "$remote_absent_marker" ]; then
  rm -f -- "$target"
fi
rm -f -- "$remote_tmp" "$remote_absent_marker"
SH
  elif [[ "$remote_rollback_mode" == "revoke" && -n "$candidate" ]]; then
    remote_root_sh "$remote_revoked" <<'SH'
set -eu
remote_revoked=$1
target=/root/ceavpn-lte-exit.env
if [ -e "$remote_revoked" ]; then
  mv "$remote_revoked" "$target"
fi
SH
  fi
  if [[ "$rollback_mode" == "delete" && -n "$token" && -n "$username" ]]; then
    authorized_request DELETE "/api/user/$username" \
      "$work_dir/rollback-response.json" >/dev/null
  elif [[ "$rollback_mode" == "restore" && -n "$token" &&
    -n "$username" && -s "$work_dir/restore.json" ]]; then
    authorized_request PUT "/api/user/$username" \
      "$work_dir/rollback-response.json" "$work_dir/restore.json" >/dev/null
  fi
  set -e
}

cleanup() {
  status=$?
  if (( status != 0 )); then
    rollback
  fi
  unset MARZBAN_BOT_USERNAME MARZBAN_BOT_PASSWORD FALLBACK_WS_PATH
  unset CEAVPN_SUB_DOMAIN
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    find "$work_dir" -type f -delete
    rmdir "$work_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

command="${1:-}"
shift || true
gateway_id=""
candidate=""
while (($#)); do
  case "$1" in
    --gateway-id)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      gateway_id="$2"
      shift 2
      ;;
    --candidate)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      candidate="$2"
      shift 2
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$gateway_id" =~ ^[a-z0-9][a-z0-9_-]{1,23}$ ]]; then
  echo "gateway ID must be 2-24 lowercase safe characters" >&2
  exit 2
fi
if [[ -n "$candidate" ]] &&
  [[ ! "$candidate" =~ ^root@([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9.-]{0,251}[A-Za-z0-9])$ ]]; then
  echo "candidate must be root@HOST" >&2
  exit 2
fi
if [[ "$command" == "create" && -z "$candidate" ]]; then
  echo "create requires --candidate root@HOST" >&2
  exit 2
fi
if [[ "$command" != "create" && "$command" != "status" && "$command" != "revoke" ]]; then
  usage >&2
  exit 2
fi

install -d -o root -g root -m 0700 "$state_dir"
state_file="$state_dir/${gateway_id}.env"
username="cea_relay_${gateway_id//-/_}"

if [[ "$command" == "status" ]]; then
  if [[ ! -s "$state_file" ]]; then
    echo "absent"
    exit 0
  fi
  CEAVPN_RELAY_STATE_FILE="$state_file" \
  CEAVPN_RELAY_EXPECTED_USERNAME="$username" \
    python3 - <<'PY'
import os
import re
import shlex
import uuid
from pathlib import Path

values = {}
for raw_line in Path(os.environ["CEAVPN_RELAY_STATE_FILE"]).read_text(
    encoding="utf-8"
).splitlines():
    if not raw_line or "=" not in raw_line:
        raise SystemExit("invalid dedicated relay state")
    key, encoded = raw_line.split("=", 1)
    if key in values or key not in {
        "CEAVPN_RELAY_STATUS",
        "CEAVPN_RELAY_USERNAME",
        "CEAVPN_RELAY_UUID",
    }:
        raise SystemExit("invalid dedicated relay state")
    parsed = shlex.split(encoded)
    if len(parsed) != 1:
        raise SystemExit("invalid dedicated relay state")
    values[key] = parsed[0]
if set(values) != {
    "CEAVPN_RELAY_STATUS",
    "CEAVPN_RELAY_USERNAME",
    "CEAVPN_RELAY_UUID",
}:
    raise SystemExit("invalid dedicated relay state")
if (
    values["CEAVPN_RELAY_STATUS"] not in {"active", "revoked"}
    or values["CEAVPN_RELAY_USERNAME"]
    != os.environ["CEAVPN_RELAY_EXPECTED_USERNAME"]
    or not re.fullmatch(r"cea_relay_[a-z0-9_]{2,24}", values["CEAVPN_RELAY_USERNAME"])
):
    raise SystemExit("invalid dedicated relay state")
try:
    identifier = uuid.UUID(values["CEAVPN_RELAY_UUID"])
except ValueError:
    raise SystemExit("invalid dedicated relay state")
if identifier.version != 4:
    raise SystemExit("invalid dedicated relay state")
print(f"local_state={values['CEAVPN_RELAY_STATUS']}")
PY
  exit 0
fi

for path in "$admin_file" "$fallback_file" "$node_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done
for tool in curl jq python3 scp ssh; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "missing required command: $tool" >&2
    exit 1
  fi
done

# shellcheck disable=SC1090
source "$admin_file"
# shellcheck disable=SC1090
source "$fallback_file"
# shellcheck disable=SC1090
source "$node_file"
: "${MARZBAN_BOT_USERNAME:?MARZBAN_BOT_USERNAME is required}"
: "${MARZBAN_BOT_PASSWORD:?MARZBAN_BOT_PASSWORD is required}"
: "${FALLBACK_WS_PATH:?FALLBACK_WS_PATH is required}"
: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
if [[ ! "$FALLBACK_WS_PATH" =~ ^/ws-[0-9a-f]{48}$ ]]; then
  echo "invalid exit WebSocket path" >&2
  exit 1
fi

work_dir="$(mktemp -d /run/ceavpn-relay.XXXXXX)"
chmod 0700 "$work_dir"
export MARZBAN_BOT_USERNAME MARZBAN_BOT_PASSWORD
token="$(get_token)"
unset MARZBAN_BOT_PASSWORD

existing_code="$(authorized_request GET "/api/user/$username" "$work_dir/existing.json")"
if [[ "$existing_code" != "200" && "$existing_code" != "404" ]]; then
  echo "could not inspect dedicated relay account" >&2
  exit 1
fi

relay_uuid=""
relay_state_status=""
if [[ -s "$state_file" ]]; then
  # shellcheck disable=SC1090
  source "$state_file"
  relay_uuid="${CEAVPN_RELAY_UUID:-}"
  relay_state_status="${CEAVPN_RELAY_STATUS:-}"
  if [[ "${CEAVPN_RELAY_USERNAME:-}" != "$username" ]] ||
    [[ "$relay_state_status" != "active" && "$relay_state_status" != "revoked" ]] ||
    ! python3 - "$relay_uuid" <<'PY'
import sys
import uuid

value = uuid.UUID(sys.argv[1])
raise SystemExit(0 if value.version == 4 else 1)
PY
  then
    echo "invalid dedicated relay state" >&2
    exit 1
  fi
fi

if [[ "$command" == "revoke" ]]; then
  if [[ -z "$relay_uuid" ]]; then
    if [[ "$existing_code" == "200" ]]; then
      echo "relay account exists without trusted local state; refusing revoke" >&2
      exit 1
    fi
    echo "dedicated whitelist relay is absent"
    exit 0
  fi
  if [[ "$existing_code" == "200" ]]; then
    if ! jq -e --arg uuid "$relay_uuid" --arg tag "$fallback_tag" '
      .proxies.vless.id == $uuid and
      .proxies.vless.flow == "" and
      .inbounds.vless == [$tag]
    ' "$work_dir/existing.json" >/dev/null; then
      echo "existing relay account does not match trusted state" >&2
      exit 1
    fi
    jq '{
      proxies, inbounds, expire, data_limit, data_limit_reset_strategy,
      status, note
    }' "$work_dir/existing.json" > "$work_dir/restore.json"
    printf '{"status":"disabled"}' > "$work_dir/revoke.json"
    rollback_mode="restore"
    revoke_code="$(
      authorized_request PUT "/api/user/$username" \
        "$work_dir/revoke-response.json" "$work_dir/revoke.json"
    )"
    if [[ "$revoke_code" != "200" ]]; then
      echo "could not revoke dedicated relay account" >&2
      exit 1
    fi
  fi
  if [[ -n "$candidate" ]]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ).$$"
    remote_revoked="/root/ceavpn-lte-exit.env.revoked.${stamp}"
    remote_rollback_mode="revoke"
    remote_root_sh "$remote_revoked" <<'SH'
set -eu
remote_revoked=$1
target=/root/ceavpn-lte-exit.env
if [ -e "$target" ]; then
  mv "$target" "$remote_revoked"
fi
SH
  fi
  printf \
    'CEAVPN_RELAY_STATUS=%q\nCEAVPN_RELAY_USERNAME=%q\nCEAVPN_RELAY_UUID=%q\n' \
    "revoked" "$username" "$relay_uuid" >"$work_dir/state.env"
  chmod 0600 "$work_dir/state.env"
  mv "$work_dir/state.env" "$state_file"
  rollback_mode=""
  remote_rollback_mode=""
  echo "dedicated whitelist relay revoked"
  exit 0
fi

if [[ -z "$relay_uuid" && "$existing_code" == "200" ]]; then
  echo "relay account exists without trusted local state; refusing adoption" >&2
  exit 1
elif [[ -z "$relay_uuid" ]]; then
  relay_uuid="$(cat /proc/sys/kernel/random/uuid)"
fi
if [[ "$existing_code" == "200" ]] &&
  ! jq -e --arg uuid "$relay_uuid" --arg tag "$fallback_tag" '
    .proxies.vless.id == $uuid and
    .proxies.vless.flow == "" and
    .inbounds.vless == [$tag]
  ' "$work_dir/existing.json" >/dev/null; then
  echo "existing relay account does not match trusted state" >&2
  exit 1
fi

jq -n \
  --arg username "$username" \
  --arg uuid "$relay_uuid" \
  --arg tag "$fallback_tag" \
  '{
    username: $username,
    proxies: {vless: {id: $uuid, flow: ""}},
    inbounds: {vless: [$tag]},
    expire: 0,
    data_limit: 0,
    data_limit_reset_strategy: "no_reset",
    status: "active",
    note: "CEA VPN dedicated whitelist relay"
  }' > "$work_dir/desired.json"

if [[ "$existing_code" == "404" ]]; then
  rollback_mode="delete"
  update_code="$(
    authorized_request POST /api/user \
      "$work_dir/update-response.json" "$work_dir/desired.json"
  )"
  if [[ "$update_code" != "200" && "$update_code" != "201" ]]; then
    echo "could not create dedicated relay account" >&2
    exit 1
  fi
else
  jq '{
    proxies, inbounds, expire, data_limit, data_limit_reset_strategy,
    status, note
  }' "$work_dir/existing.json" > "$work_dir/restore.json"
  rollback_mode="restore"
  update_code="$(
    authorized_request PUT "/api/user/$username" \
      "$work_dir/update-response.json" "$work_dir/desired.json"
  )"
  if [[ "$update_code" != "200" ]]; then
    echo "could not update dedicated relay account" >&2
    exit 1
  fi
fi

if ! jq -e --arg uuid "$relay_uuid" --arg tag "$fallback_tag" '
  .status == "active" and
  .proxies.vless.id == $uuid and
  .proxies.vless.flow == "" and
  .inbounds.vless == [$tag]
' "$work_dir/update-response.json" >/dev/null; then
  echo "dedicated relay verification failed" >&2
  exit 1
fi

printf \
  'CEAVPN_RELAY_STATUS=%q\nCEAVPN_RELAY_USERNAME=%q\nCEAVPN_RELAY_UUID=%q\n' \
  "active" "$username" "$relay_uuid" >"$work_dir/state.env"
chmod 0600 "$work_dir/state.env"

cat > "$work_dir/candidate.env" <<EOF
CEAVPN_LTE_EXIT_ADDRESS=${CEAVPN_SUB_DOMAIN}
CEAVPN_LTE_EXIT_PORT=8443
CEAVPN_LTE_EXIT_UUID=${relay_uuid}
CEAVPN_LTE_EXIT_SNI=${CEAVPN_SUB_DOMAIN}
CEAVPN_LTE_EXIT_HOST=${CEAVPN_SUB_DOMAIN}
CEAVPN_LTE_EXIT_PATH=${FALLBACK_WS_PATH}
EOF
chmod 0600 "$work_dir/candidate.env"

remote_tmp="/tmp/.ceavpn-lte-exit.env.${gateway_id}.new"
scp -o BatchMode=yes -p "$work_dir/candidate.env" "$candidate:$remote_tmp"
stamp="$(date -u +%Y%m%dT%H%M%SZ).$$"
remote_backup="/root/ceavpn-lte-exit.env.before.${stamp}"
remote_absent_marker="/root/.ceavpn-lte-exit.env.absent.${stamp}"
remote_rollback_mode="install"
remote_root_sh "$remote_tmp" "$remote_backup" "$remote_absent_marker" <<'SH'
set -eu
remote_tmp=$1
remote_backup=$2
remote_absent_marker=$3
target=/root/ceavpn-lte-exit.env
if [ -e "$target" ]; then
  cp -p "$target" "$remote_backup"
else
  : >"$remote_absent_marker"
  chmod 0600 "$remote_absent_marker"
fi
install -o root -g root -m 0600 "$remote_tmp" "$target"
rm -f -- "$remote_tmp"
SH

mv "$work_dir/state.env" "$state_file"
chmod 0600 "$state_file"
rollback_mode=""
remote_rollback_mode=""
if ! remote_root_sh "$remote_backup" "$remote_absent_marker" <<'SH'
set -eu
rm -f -- "$1" "$2"
SH
then
  echo "warning: relay committed but remote rollback marker cleanup failed" >&2
fi
echo "dedicated whitelist relay configured and transferred"
