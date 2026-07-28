#!/usr/bin/env bash
set -Eeuo pipefail

# Marzban v0.8.4 restricts /api/hosts to sudo admins. Keep these credentials
# separate from /root/ceavpn-admin.env, which belongs to the non-sudo worker.
admin_file="/root/ceavpn-sudo-admin.env"
fallback_file="/root/ceavpn-fallback.env"
node_file="/root/ceavpn-node.env"
published_hosts_file="${CEAVPN_PUBLISHED_HOSTS_FILE:-/root/ceavpn-published-hosts.json}"
api_base="http://127.0.0.1:8000"
reality_tag="VLESS TCP REALITY"
fallback_tag="VLESS WS TLS FALLBACK"
work_dir=""
rollback_needed=0
curl_common=(-fsS --connect-timeout 5 --max-time 20)

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

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
    echo "host override rollback failed; manual recovery required" >&2
  else
    echo "host override update failed; previous target overrides restored" >&2
  fi
}

cleanup() {
  status=$?
  if (( status != 0 )); then
    rollback_hosts
  fi
  unset MARZBAN_SUDO_PASSWORD MARZBAN_SUDO_USERNAME FALLBACK_WS_PATH
  unset CEAVPN_HOSTS_BASELINE CEAVPN_HOSTS_PAYLOAD
  unset CEAVPN_HOSTS_ORIGINAL CEAVPN_HOSTS_RESULT
  unset CEAVPN_REALITY_TAG CEAVPN_FALLBACK_TAG
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    find "$work_dir" -type f -delete
    rmdir "$work_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

for path in "$admin_file" "$fallback_file" "$node_file"; do
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
source "$fallback_file"
# shellcheck disable=SC1090
source "$node_file"

: "${MARZBAN_SUDO_USERNAME:?MARZBAN_SUDO_USERNAME is required}"
: "${MARZBAN_SUDO_PASSWORD:?MARZBAN_SUDO_PASSWORD is required}"
: "${FALLBACK_WS_PATH:?FALLBACK_WS_PATH is required}"
: "${CEAVPN_PUBLIC_IP:?CEAVPN_PUBLIC_IP is required}"
: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
: "${CEAVPN_COVER_DOMAIN:?CEAVPN_COVER_DOMAIN is required}"
: "${CEAVPN_REGION_REMARK:?CEAVPN_REGION_REMARK is required}"

if [[ ! "$FALLBACK_WS_PATH" =~ ^/ws-[0-9a-f]{48}$ ]]; then
  echo "invalid fallback WebSocket path" >&2
  exit 1
fi

work_dir="$(mktemp -d /run/ceavpn-hosts.XXXXXX)"
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

printf 'header = "Authorization: Bearer %s"\n' "$token" \
  > "$work_dir/auth.curl"
unset token MARZBAN_SUDO_PASSWORD

curl "${curl_common[@]}" \
  --config "$work_dir/auth.curl" \
  "$api_base/api/hosts" \
  -o "$work_dir/baseline.json"

export CEAVPN_HOSTS_BASELINE="$work_dir/baseline.json"
export CEAVPN_HOSTS_PAYLOAD="$work_dir/payload.json"
export CEAVPN_HOSTS_ORIGINAL="$work_dir/original.json"
export CEAVPN_REALITY_TAG="$reality_tag"
export CEAVPN_FALLBACK_TAG="$fallback_tag"
export CEAVPN_PUBLIC_IP CEAVPN_SUB_DOMAIN CEAVPN_COVER_DOMAIN
export CEAVPN_REGION_REMARK
export FALLBACK_WS_PATH
export CEAVPN_PUBLISHED_HOSTS_FILE="$published_hosts_file"

change_state="$(python3 - <<'PY'
import json
import os
from pathlib import Path

reality_tag = os.environ["CEAVPN_REALITY_TAG"]
fallback_tag = os.environ["CEAVPN_FALLBACK_TAG"]
fallback_path = os.environ["FALLBACK_WS_PATH"]
public_ip = os.environ["CEAVPN_PUBLIC_IP"]
sub_domain = os.environ["CEAVPN_SUB_DOMAIN"]
cover_domain = os.environ["CEAVPN_COVER_DOMAIN"]
region_remark = os.environ["CEAVPN_REGION_REMARK"]
published_hosts_file = Path(os.environ["CEAVPN_PUBLISHED_HOSTS_FILE"])
baseline = json.loads(
    Path(os.environ["CEAVPN_HOSTS_BASELINE"]).read_text(encoding="utf-8")
)

if not isinstance(baseline, dict):
    raise SystemExit("invalid Marzban hosts response")
for tag in (reality_tag, fallback_tag):
    if tag not in baseline or not isinstance(baseline[tag], list):
        raise SystemExit(f"required inbound is missing: {tag}")

fallback_hosts = [{
    "remark": region_remark,
    "address": sub_domain,
    "port": 8443,
    "sni": sub_domain,
    "host": sub_domain,
    "path": fallback_path,
}]
if published_hosts_file.is_file():
    configured_hosts = json.loads(
        published_hosts_file.read_text(encoding="utf-8")
    )
    if not isinstance(configured_hosts, list) or not configured_hosts:
        raise SystemExit("invalid published VPN hosts file")
    fallback_hosts = configured_hosts

normalized_fallback_hosts = []
seen_remarks = set()
for host_config in fallback_hosts:
    if not isinstance(host_config, dict):
        raise SystemExit("invalid published VPN host")
    remark = str(host_config.get("remark") or "").strip()
    address = str(host_config.get("address") or "").strip()
    sni = str(host_config.get("sni") or address).strip()
    host = str(host_config.get("host") or sni).strip()
    path = str(host_config.get("path") or "").strip()
    port = host_config.get("port", 8443)
    if (
        not remark
        or remark in seen_remarks
        or not address
        or not sni
        or not host
        or not isinstance(port, int)
        or not (1 <= port <= 65535)
        or not path.startswith("/ws-")
        or len(path) != 52
        or any(character not in "0123456789abcdef" for character in path[4:])
    ):
        raise SystemExit("invalid published VPN host fields")
    seen_remarks.add(remark)
    normalized_fallback_hosts.append({
        "remark": remark,
        "address": address,
        "port": port,
        "sni": sni,
        "host": host,
        "path": path,
        "security": "tls",
        "alpn": "http/1.1",
        "fingerprint": "chrome",
        "allowinsecure": False,
        "is_disabled": False,
        "mux_enable": False,
        "fragment_setting": None,
        "noise_setting": None,
        "random_user_agent": False,
        "use_sni_as_host": False,
    })

desired = {
    reality_tag: [{
        "remark": f"{region_remark} (Reality)",
        "address": public_ip,
        "port": 443,
        "sni": cover_domain,
        "host": None,
        "path": None,
        "security": "inbound_default",
        "alpn": "",
        "fingerprint": "chrome",
        "allowinsecure": False,
        # Reality remains configured on the same VPS as a rollback option, but
        # Marzban must not publish it to clients while Happ cannot use it
        # reliably. Disabled hosts are omitted from generated subscriptions.
        "is_disabled": True,
        "mux_enable": False,
        "fragment_setting": None,
        "noise_setting": None,
        "random_user_agent": False,
        "use_sni_as_host": False,
    }],
    # Happ uses the first flag emoji in each remark as the server icon.
    fallback_tag: normalized_fallback_hosts,
}

keys = tuple(next(iter(desired.values()))[0].keys())

def matches(actual_hosts, expected_hosts):
    if len(actual_hosts) != len(expected_hosts):
        return False
    return all(
        {key: actual.get(key) for key in keys} == expected
        for actual, expected in zip(actual_hosts, expected_hosts)
    )

Path(os.environ["CEAVPN_HOSTS_PAYLOAD"]).write_text(
    json.dumps(desired, ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
Path(os.environ["CEAVPN_HOSTS_ORIGINAL"]).write_text(
    json.dumps(
        {reality_tag: baseline[reality_tag], fallback_tag: baseline[fallback_tag]},
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    encoding="utf-8",
)

already_configured = all(
    matches(baseline[tag], desired[tag]) for tag in (reality_tag, fallback_tag)
)
print("unchanged" if already_configured else "changed")
PY
)"
export -n FALLBACK_WS_PATH

chmod 0600 \
  "$work_dir/baseline.json" \
  "$work_dir/payload.json" \
  "$work_dir/original.json"

if [[ "$change_state" == "unchanged" ]]; then
  echo "Marzban host overrides are already configured"
  exit 0
fi
if [[ "$change_state" != "changed" ]]; then
  echo "could not determine Marzban host override state" >&2
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

reality_tag = os.environ["CEAVPN_REALITY_TAG"]
fallback_tag = os.environ["CEAVPN_FALLBACK_TAG"]
target_tags = {reality_tag, fallback_tag}

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

foreign_before = {
    tag: hosts for tag, hosts in baseline.items() if tag not in target_tags
}
foreign_after = {
    tag: hosts for tag, hosts in result.items() if tag not in target_tags
}
if foreign_after != foreign_before:
    raise SystemExit("unmanaged Marzban host overrides changed")

keys = tuple(desired[reality_tag][0].keys())
for tag in target_tags:
    actual_hosts = result.get(tag)
    if (
        not isinstance(actual_hosts, list)
        or len(actual_hosts) != len(desired[tag])
    ):
        raise SystemExit(f"invalid updated host override count: {tag}")
    actual = [
        {key: host.get(key) for key in keys}
        for host in actual_hosts
    ]
    if actual != desired[tag]:
        raise SystemExit(f"Marzban host override verification failed: {tag}")
PY

rollback_needed=0
echo "Marzban WS/TLS profiles configured; Reality profile hidden"
