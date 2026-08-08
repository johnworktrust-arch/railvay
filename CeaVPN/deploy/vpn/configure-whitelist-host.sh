#!/usr/bin/env bash
set -Eeuo pipefail

admin_file="/root/ceavpn-sudo-admin.env"
xhttp_file="/root/ceavpn-xhttp.env"
reality_file="/root/ceavpn-reality.env"
lte_exit_file="/root/ceavpn-lte-exit.env"
node_file="/root/ceavpn-node.env"
qualification_file="${CEAVPN_WHITELIST_QUALIFICATION_FILE:-/root/ceavpn-whitelist-qualified.json}"
gate_lock_file="/run/lock/ceavpn-whitelist-gate.lock"
api_base="http://127.0.0.1:8000"
xhttp_tag="VLESS XHTTP REALITY"
work_dir=""
rollback_needed=0
curl_common=(--noproxy '*' -fsS --connect-timeout 5 --max-time 20)

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

acquire_gate_lock() {
  local inherited_fd="${CEAVPN_WHITELIST_GATE_LOCK_FD:-}"
  local inherited_target=""
  if [[ "$inherited_fd" =~ ^[0-9]+$ ]] &&
    [[ -e "/proc/$$/fd/$inherited_fd" ]]; then
    inherited_target="$(readlink -f "/proc/$$/fd/$inherited_fd" 2>/dev/null || true)"
  fi
  if [[ "$inherited_target" == "$gate_lock_file" ]] &&
    flock -n "$inherited_fd"; then
    return 0
  fi
  unset CEAVPN_WHITELIST_GATE_LOCK_FD
  install -d -o root -g root -m 0755 "$(dirname "$gate_lock_file")"
  gate_lock_fd="8"
  exec 8>"$gate_lock_file"
  chmod 0600 "$gate_lock_file"
  flock -x "$gate_lock_fd"
  export CEAVPN_WHITELIST_GATE_LOCK_FD="$gate_lock_fd"
}

if ! command -v flock >/dev/null 2>&1; then
  echo "missing required command: flock" >&2
  exit 1
fi
acquire_gate_lock

rollback_hosts() {
  if (( ! rollback_needed )) || [[ -z "$work_dir" ]]; then
    return
  fi
  if [[ ! -s "$work_dir/auth.curl" || ! -s "$work_dir/original.json" ]]; then
    return
  fi
  set +e
  curl "${curl_common[@]}" -X PUT \
    --config "$work_dir/auth.curl" \
    -H 'Content-Type: application/json' \
    --data-binary "@$work_dir/original.json" \
    "$api_base/api/hosts" \
    -o /dev/null
  rollback_status=$?
  set -e
  if (( rollback_status != 0 )); then
    echo "whitelist host rollback failed; manual recovery required" >&2
  fi
}

cleanup() {
  status=$?
  if (( status != 0 )); then
    rollback_hosts
  fi
  unset MARZBAN_SUDO_USERNAME MARZBAN_SUDO_PASSWORD XHTTP_PATH
  unset CEAVPN_HOSTS_BASELINE CEAVPN_HOSTS_PAYLOAD CEAVPN_HOSTS_ORIGINAL
  unset CEAVPN_HOSTS_RESULT CEAVPN_WHITELIST_TAG
  unset CEAVPN_PUBLIC_IP CEAVPN_SUB_DOMAIN CEAVPN_COVER_DOMAIN
  unset CEAVPN_REGION_REMARK
  unset CEAVPN_SERVER_CODE
  unset CEAVPN_WHITELIST_QUALIFICATION_FILE
  unset REALITY_PUBLIC_KEY REALITY_SHORT_ID XHTTP_PATH
  unset CEAVPN_LTE_EXIT_ADDRESS CEAVPN_LTE_EXIT_PORT CEAVPN_LTE_EXIT_UUID
  unset CEAVPN_LTE_EXIT_SNI CEAVPN_LTE_EXIT_HOST CEAVPN_LTE_EXIT_PATH
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    find "$work_dir" -type f -delete
    rmdir "$work_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

for path in \
  "$admin_file" \
  "$xhttp_file" \
  "$reality_file" \
  "$lte_exit_file" \
  "$node_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done
for command in curl jq python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "missing required command: $command" >&2
    exit 1
  fi
done

# shellcheck disable=SC1090
source "$admin_file"
# shellcheck disable=SC1090
source "$xhttp_file"
# shellcheck disable=SC1090
source "$reality_file"
# shellcheck disable=SC1090
source "$lte_exit_file"
# shellcheck disable=SC1090
source "$node_file"

: "${MARZBAN_SUDO_USERNAME:?MARZBAN_SUDO_USERNAME is required}"
: "${MARZBAN_SUDO_PASSWORD:?MARZBAN_SUDO_PASSWORD is required}"
: "${XHTTP_PATH:?XHTTP_PATH is required}"
: "${CEAVPN_PUBLIC_IP:?CEAVPN_PUBLIC_IP is required}"
: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
: "${CEAVPN_COVER_DOMAIN:?CEAVPN_COVER_DOMAIN is required}"
: "${CEAVPN_REGION_REMARK:?CEAVPN_REGION_REMARK is required}"
: "${CEAVPN_SERVER_CODE:?CEAVPN_SERVER_CODE is required}"
: "${REALITY_PUBLIC_KEY:?REALITY_PUBLIC_KEY is required}"
: "${REALITY_SHORT_ID:?REALITY_SHORT_ID is required}"
: "${CEAVPN_LTE_EXIT_ADDRESS:?CEAVPN_LTE_EXIT_ADDRESS is required}"
: "${CEAVPN_LTE_EXIT_PORT:?CEAVPN_LTE_EXIT_PORT is required}"
: "${CEAVPN_LTE_EXIT_UUID:?CEAVPN_LTE_EXIT_UUID is required}"
: "${CEAVPN_LTE_EXIT_SNI:?CEAVPN_LTE_EXIT_SNI is required}"
: "${CEAVPN_LTE_EXIT_HOST:?CEAVPN_LTE_EXIT_HOST is required}"
: "${CEAVPN_LTE_EXIT_PATH:?CEAVPN_LTE_EXIT_PATH is required}"
if [[ "${CEAVPN_NODE_MODE:-direct}" != "whitelist" ]]; then
  echo "whitelist host configuration requires CEAVPN_NODE_MODE=whitelist" >&2
  exit 1
fi
if [[ ! "$XHTTP_PATH" =~ ^/xhttp-[0-9a-f]{48}$ ]]; then
  echo "invalid XHTTP path" >&2
  exit 1
fi
if [[ ! "$CEAVPN_SERVER_CODE" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
  echo "invalid CEAVPN_SERVER_CODE" >&2
  exit 1
fi

work_dir="$(mktemp -d /run/ceavpn-whitelist-hosts.XXXXXX)"
chmod 0700 "$work_dir"

export MARZBAN_SUDO_USERNAME MARZBAN_SUDO_PASSWORD
token="$(
  python3 - <<'PY' |
import os
import urllib.parse

print(urllib.parse.urlencode({
    "username": os.environ["MARZBAN_SUDO_USERNAME"],
    "password": os.environ["MARZBAN_SUDO_PASSWORD"],
    "grant_type": "password",
}))
PY
  curl "${curl_common[@]}" -X POST "$api_base/api/admin/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-binary @- |
  jq -er '.access_token'
)"
printf 'header = "Authorization: Bearer %s"\n' "$token" > "$work_dir/auth.curl"
unset token MARZBAN_SUDO_PASSWORD

curl "${curl_common[@]}" \
  --config "$work_dir/auth.curl" \
  "$api_base/api/hosts" \
  -o "$work_dir/baseline.json"

export CEAVPN_HOSTS_BASELINE="$work_dir/baseline.json"
export CEAVPN_HOSTS_PAYLOAD="$work_dir/payload.json"
export CEAVPN_HOSTS_ORIGINAL="$work_dir/original.json"
export CEAVPN_WHITELIST_TAG="$xhttp_tag"
export CEAVPN_PUBLIC_IP CEAVPN_SUB_DOMAIN CEAVPN_COVER_DOMAIN
export CEAVPN_REGION_REMARK XHTTP_PATH
export CEAVPN_SERVER_CODE
export CEAVPN_WHITELIST_QUALIFICATION_FILE="$qualification_file"
export REALITY_PUBLIC_KEY REALITY_SHORT_ID
export CEAVPN_LTE_EXIT_ADDRESS CEAVPN_LTE_EXIT_PORT CEAVPN_LTE_EXIT_UUID
export CEAVPN_LTE_EXIT_SNI CEAVPN_LTE_EXIT_HOST CEAVPN_LTE_EXIT_PATH

change_state="$(python3 - <<'PY'
import json
import hashlib
import ipaddress
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

tag = os.environ["CEAVPN_WHITELIST_TAG"]
baseline = json.loads(
    Path(os.environ["CEAVPN_HOSTS_BASELINE"]).read_text(encoding="utf-8")
)
if not isinstance(baseline, dict) or not isinstance(baseline.get(tag), list):
    raise SystemExit(f"required inbound is missing: {tag}")

try:
    exit_port = int(os.environ["CEAVPN_LTE_EXIT_PORT"])
    exit_uuid = str(uuid.UUID(os.environ["CEAVPN_LTE_EXIT_UUID"]))
    public_ip = str(ipaddress.ip_address(os.environ["CEAVPN_PUBLIC_IP"]))
except ValueError as exc:
    raise SystemExit("invalid whitelist fingerprint input") from exc
if not 1 <= exit_port <= 65535:
    raise SystemExit("invalid whitelist fingerprint input")
if (
    re.fullmatch(r"/xhttp-[0-9a-f]{48}", os.environ["XHTTP_PATH"]) is None
    or re.fullmatch(
        r"/ws-[0-9a-f]{48}", os.environ["CEAVPN_LTE_EXIT_PATH"]
    )
    is None
):
    raise SystemExit("invalid whitelist fingerprint input")
fingerprint_payload = {
    "server_code": os.environ["CEAVPN_SERVER_CODE"],
    "public_ip": public_ip,
    "cover_domain": os.environ["CEAVPN_COVER_DOMAIN"],
    "subscription_domain": os.environ["CEAVPN_SUB_DOMAIN"],
    "xhttp_path": os.environ["XHTTP_PATH"],
    "reality_public_key": os.environ["REALITY_PUBLIC_KEY"],
    "reality_short_id": os.environ["REALITY_SHORT_ID"],
    "exit_address": os.environ["CEAVPN_LTE_EXIT_ADDRESS"],
    "exit_port": exit_port,
    "exit_uuid": exit_uuid,
    "exit_sni": os.environ["CEAVPN_LTE_EXIT_SNI"],
    "exit_host": os.environ["CEAVPN_LTE_EXIT_HOST"],
    "exit_path": os.environ["CEAVPN_LTE_EXIT_PATH"],
}
current_fingerprint = hashlib.sha256(
    json.dumps(
        fingerprint_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
).hexdigest()

qualified = False
qualification_path = Path(os.environ["CEAVPN_WHITELIST_QUALIFICATION_FILE"])
if qualification_path.is_file():
    try:
        record = json.loads(qualification_path.read_text(encoding="utf-8"))
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
        valid_until = datetime.fromisoformat(
            record["valid_until"].replace("Z", "+00:00")
        )
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        required_checks = {
            "xhttp_profile_connected",
            "dns_through_tunnel",
            "telegram_through_tunnel",
            "https_through_tunnel",
            "transfer_over_1mib",
        }
        qualified = (
            isinstance(record, dict)
            and set(record) == allowed
            and all(
                isinstance(record[key], str)
                for key in allowed - {"checks"}
            )
            and isinstance(record["checks"], list)
            and all(isinstance(item, str) for item in record["checks"])
            and record["status"] == "passed"
            and record["evidence"] == "restricted-sim-xhttp-tunnel-worked"
            and record["config_fingerprint"] == current_fingerprint
            and record["probe_url"]
            == (
                f"https://{os.environ['CEAVPN_SUB_DOMAIN']}:8443"
                "/.well-known/ceavpn-whitelist-probe"
            )
            and set(record["checks"]) == required_checks
            and len(record["checks"]) == len(required_checks)
            and valid_until.astimezone(timezone.utc)
            > datetime.now(timezone.utc)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        qualified = False

desired = {
    tag: [{
        "remark": os.environ["CEAVPN_REGION_REMARK"],
        "address": os.environ["CEAVPN_PUBLIC_IP"],
        "port": 443,
        "sni": os.environ["CEAVPN_COVER_DOMAIN"],
        "host": None,
        "path": os.environ["XHTTP_PATH"],
        "security": "inbound_default",
        "alpn": "",
        "fingerprint": "chrome",
        "allowinsecure": False,
        "is_disabled": not qualified,
        "mux_enable": False,
        "fragment_setting": None,
        "noise_setting": None,
        "random_user_agent": False,
        "use_sni_as_host": False,
    }]
}
keys = tuple(desired[tag][0])
actual = baseline[tag]
matches = (
    len(actual) == 1
    and {key: actual[0].get(key) for key in keys} == desired[tag][0]
)
Path(os.environ["CEAVPN_HOSTS_PAYLOAD"]).write_text(
    json.dumps(desired, ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
Path(os.environ["CEAVPN_HOSTS_ORIGINAL"]).write_text(
    json.dumps({tag: baseline[tag]}, ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
print("unchanged" if matches else "changed")
PY
)"
unset XHTTP_PATH
chmod 0600 "$work_dir/baseline.json" "$work_dir/payload.json" "$work_dir/original.json"

if [[ "$change_state" == "unchanged" ]]; then
  echo "whitelist host publication gate is already configured"
  exit 0
fi
if [[ "$change_state" != "changed" ]]; then
  echo "could not determine whitelist host state" >&2
  exit 1
fi

rollback_needed=1
curl "${curl_common[@]}" -X PUT \
  --config "$work_dir/auth.curl" \
  -H 'Content-Type: application/json' \
  --data-binary "@$work_dir/payload.json" \
  "$api_base/api/hosts" \
  -o "$work_dir/result.json"

export CEAVPN_HOSTS_RESULT="$work_dir/result.json"
python3 - <<'PY'
import json
import os
from pathlib import Path

tag = os.environ["CEAVPN_WHITELIST_TAG"]
baseline = json.loads(
    Path(os.environ["CEAVPN_HOSTS_BASELINE"]).read_text(encoding="utf-8")
)
desired = json.loads(
    Path(os.environ["CEAVPN_HOSTS_PAYLOAD"]).read_text(encoding="utf-8")
)
result = json.loads(
    Path(os.environ["CEAVPN_HOSTS_RESULT"]).read_text(encoding="utf-8")
)
if not isinstance(result, dict):
    raise SystemExit("invalid Marzban update response")
foreign_before = {key: value for key, value in baseline.items() if key != tag}
foreign_after = {key: value for key, value in result.items() if key != tag}
if foreign_after != foreign_before:
    raise SystemExit("unmanaged Marzban host overrides changed")
keys = tuple(desired[tag][0])
actual = result.get(tag)
if (
    not isinstance(actual, list)
    or len(actual) != 1
    or {key: actual[0].get(key) for key in keys} != desired[tag][0]
):
    raise SystemExit("whitelist host override verification failed")
PY

rollback_needed=0
if jq -e --arg tag "$xhttp_tag" \
  '.[$tag] | type == "array" and length == 1 and
   .[0].is_disabled == false' \
  "$work_dir/payload.json" >/dev/null 2>&1; then
  echo "whitelist XHTTP profile enabled after explicit qualification"
else
  echo "whitelist XHTTP profile remains disabled pending restricted-SIM qualification"
fi
