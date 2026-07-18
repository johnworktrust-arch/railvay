#!/usr/bin/env bash
set -euo pipefail

xray_template="${1:-/opt/marzban/xray_config.template.json}"
nginx_template="${2:-/opt/marzban/nginx.template.conf}"
nginx_output="${3:-/etc/nginx/sites-enabled/ceavpn}"
key_file="/root/ceavpn-reality-keys.txt"
reality_file="/root/ceavpn-reality.env"
fallback_file="/root/ceavpn-fallback.env"
data_dir="/var/lib/marzban"
xray_output="$data_dir/xray_config.json"
compose_file="/opt/marzban/docker-compose.yml"
xray_new="$data_dir/xray_config.new.json"
nginx_new="${nginx_output}.new"
backup_dir="/root/ceavpn-config-backups"
nginx_test_config=""
reality_new=""
fallback_new=""

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

cleanup() {
  status=$?
  set +e
  unset CEAVPN_REALITY_PRIVATE_KEY CEAVPN_REALITY_PUBLIC_KEY
  unset CEAVPN_REALITY_SHORT_ID CEAVPN_FALLBACK_WS_PATH
  unset CEAVPN_XRAY_TEMPLATE CEAVPN_XRAY_OUTPUT
  unset CEAVPN_NGINX_TEMPLATE CEAVPN_NGINX_OUTPUT
  rm -f -- "$xray_new" "$nginx_new"
  if [[ -n "$nginx_test_config" ]]; then
    rm -f -- "$nginx_test_config"
  fi
  if [[ -n "$reality_new" ]]; then
    rm -f -- "$reality_new"
  fi
  if [[ -n "$fallback_new" ]]; then
    rm -f -- "$fallback_new"
  fi
  exit "$status"
}
trap cleanup EXIT

for path in \
  "$xray_template" \
  "$nginx_template" \
  "$key_file" \
  "$compose_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done

for command in openssl python3 nginx docker systemctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "missing required command: $command" >&2
    exit 1
  fi
done

if [[ ! -d "$(dirname "$nginx_output")" ]]; then
  echo "missing nginx output directory" >&2
  exit 1
fi
install -d -o root -g root -m 0700 "$backup_dir"

private_key="$(sed -n 's/^Private key:[[:space:]]*//p' "$key_file")"
public_key="$(sed -n 's/^Public key:[[:space:]]*//p' "$key_file")"
if [[ -z "$private_key" || -z "$public_key" ]]; then
  echo "could not parse Reality keys" >&2
  exit 1
fi

if [[ ! -e "$reality_file" ]]; then
  short_id="$(openssl rand -hex 8)"
  reality_new="$(mktemp "${reality_file}.new.XXXXXX")"
  printf 'REALITY_PUBLIC_KEY=%s\nREALITY_SHORT_ID=%s\n' \
    "$public_key" "$short_id" > "$reality_new"
  chmod 0600 "$reality_new"
  mv "$reality_new" "$reality_file"
elif [[ ! -s "$reality_file" ]]; then
  echo "Reality environment file is empty" >&2
  exit 1
fi
chmod 0600 "$reality_file"

if [[ ! -e "$fallback_file" ]]; then
  fallback_path="/ws-$(openssl rand -hex 24)"
  fallback_new="$(mktemp "${fallback_file}.new.XXXXXX")"
  printf 'FALLBACK_WS_PATH=%s\n' "$fallback_path" > "$fallback_new"
  chmod 0600 "$fallback_new"
  mv "$fallback_new" "$fallback_file"
elif [[ ! -s "$fallback_file" ]]; then
  echo "fallback environment file is empty" >&2
  exit 1
fi
chmod 0600 "$fallback_file"

# shellcheck disable=SC1090
source "$reality_file"
# shellcheck disable=SC1090
source "$fallback_file"

if [[ "$REALITY_PUBLIC_KEY" != "$public_key" ]]; then
  echo "Reality public key does not match the private-key source" >&2
  exit 1
fi
if [[ ! "$REALITY_SHORT_ID" =~ ^[0-9a-f]{16}$ ]]; then
  echo "invalid Reality short ID" >&2
  exit 1
fi
if [[ ! "$FALLBACK_WS_PATH" =~ ^/ws-[0-9a-f]{48}$ ]]; then
  echo "invalid fallback WebSocket path" >&2
  exit 1
fi

export CEAVPN_REALITY_PRIVATE_KEY="$private_key"
export CEAVPN_REALITY_PUBLIC_KEY="$REALITY_PUBLIC_KEY"
export CEAVPN_REALITY_SHORT_ID="$REALITY_SHORT_ID"
export CEAVPN_FALLBACK_WS_PATH="$FALLBACK_WS_PATH"
export CEAVPN_XRAY_TEMPLATE="$xray_template"
export CEAVPN_XRAY_OUTPUT="$xray_new"
export CEAVPN_NGINX_TEMPLATE="$nginx_template"
export CEAVPN_NGINX_OUTPUT="$nginx_new"

python3 - <<'PY'
import json
import os
import re
from pathlib import Path

xray_template = Path(os.environ["CEAVPN_XRAY_TEMPLATE"]).read_text(
    encoding="utf-8"
)
nginx_template = Path(os.environ["CEAVPN_NGINX_TEMPLATE"]).read_text(
    encoding="utf-8"
)

xray_replacements = {
    "__REALITY_PRIVATE_KEY__": os.environ["CEAVPN_REALITY_PRIVATE_KEY"],
    "__REALITY_PUBLIC_KEY__": os.environ["CEAVPN_REALITY_PUBLIC_KEY"],
    "__REALITY_SHORT_ID__": os.environ["CEAVPN_REALITY_SHORT_ID"],
    "__FALLBACK_WS_PATH__": os.environ["CEAVPN_FALLBACK_WS_PATH"],
}
nginx_replacements = {
    "__FALLBACK_WS_PATH__": os.environ["CEAVPN_FALLBACK_WS_PATH"],
}

for placeholder in xray_replacements:
    if xray_template.count(placeholder) != 1:
        raise SystemExit(f"invalid Xray placeholder count: {placeholder}")
for placeholder in nginx_replacements:
    if nginx_template.count(placeholder) != 1:
        raise SystemExit(f"invalid Nginx placeholder count: {placeholder}")

rendered_xray = xray_template
for placeholder, value in xray_replacements.items():
    rendered_xray = rendered_xray.replace(placeholder, value)

rendered_nginx = nginx_template
for placeholder, value in nginx_replacements.items():
    rendered_nginx = rendered_nginx.replace(placeholder, value)

placeholder_pattern = re.compile(r"__[A-Z0-9_]+__")
if placeholder_pattern.search(rendered_xray):
    raise SystemExit("unreplaced Xray placeholder")
if placeholder_pattern.search(rendered_nginx):
    raise SystemExit("unreplaced Nginx placeholder")

parsed_xray = json.loads(rendered_xray)
inbounds = {
    inbound.get("tag"): inbound
    for inbound in parsed_xray.get("inbounds", [])
    if isinstance(inbound, dict)
}
primary = inbounds.get("VLESS TCP REALITY", {})
fallback = inbounds.get("VLESS WS TLS FALLBACK", {})
if primary.get("listen") != "0.0.0.0" or primary.get("port") != 443:
    raise SystemExit("primary Xray listener changed unexpectedly")
if (
    fallback.get("listen") != "127.0.0.1"
    or fallback.get("port") != 10001
    or fallback.get("protocol") != "vless"
    or fallback.get("streamSettings", {}).get("network") != "ws"
    or fallback.get("streamSettings", {}).get("security") != "none"
    or fallback.get("streamSettings", {}).get("wsSettings", {}).get("path")
    != os.environ["CEAVPN_FALLBACK_WS_PATH"]
):
    raise SystemExit("invalid Xray WebSocket fallback")

required_nginx_fragments = (
    "listen 8443 ssl;",
    "listen [::]:8443 ssl;",
    "listen 127.0.0.1:9443 ssl;",
    "proxy_pass http://127.0.0.1:10001;",
    f"location = {os.environ['CEAVPN_FALLBACK_WS_PATH']} {{",
)
if any(rendered_nginx.count(fragment) != 1 for fragment in required_nginx_fragments):
    raise SystemExit("required Nginx listeners or fallback route are missing")
if "listen 2053" in rendered_nginx or "listen [::]:2053" in rendered_nginx:
    raise SystemExit("unexpected public fallback listener")

Path(os.environ["CEAVPN_XRAY_OUTPUT"]).write_text(
    rendered_xray, encoding="utf-8"
)
Path(os.environ["CEAVPN_NGINX_OUTPUT"]).write_text(
    rendered_nginx, encoding="utf-8"
)
PY

unset CEAVPN_REALITY_PRIVATE_KEY CEAVPN_REALITY_PUBLIC_KEY
unset CEAVPN_REALITY_SHORT_ID CEAVPN_FALLBACK_WS_PATH
chmod 0600 "$xray_new" "$nginx_new"

"$data_dir/xray-core/xray" run -test -c "$xray_new"
docker compose -f "$compose_file" config -q

nginx_test_config="$(mktemp /run/ceavpn-nginx-test.XXXXXX.conf)"
{
  printf 'events {}\n'
  printf 'http {\n'
  printf '    include "%s";\n' "$nginx_new"
  printf '}\n'
} > "$nginx_test_config"
nginx -t -q -p / -c "$nginx_test_config"

stamp="$(date -u +%Y%m%dT%H%M%SZ).$$"
xray_backup="${backup_dir}/xray_config.before-fallback.${stamp}.json"
nginx_backup="${backup_dir}/nginx.before-fallback.${stamp}.conf"
xray_existed=0
nginx_existed=0

if [[ -e "$xray_output" ]]; then
  cp -a "$xray_output" "$xray_backup"
  xray_existed=1
fi
if [[ -e "$nginx_output" ]]; then
  cp -a "$nginx_output" "$nginx_backup"
  nginx_existed=1
fi

restore_files() {
  if (( xray_existed )); then
    cp -a "$xray_backup" "$xray_output"
  else
    rm -f -- "$xray_output"
  fi
  if (( nginx_existed )); then
    cp -a "$nginx_backup" "$nginx_output"
  else
    rm -f -- "$nginx_output"
  fi
}

mv "$xray_new" "$xray_output"
mv "$nginx_new" "$nginx_output"

if ! nginx -t -q; then
  restore_files
  echo "Nginx validation failed; configuration restored" >&2
  exit 1
fi

if ! docker compose -f "$compose_file" up -d --force-recreate; then
  restore_files
  docker compose -f "$compose_file" up -d --force-recreate || true
  echo "Xray restart failed; configuration restored" >&2
  exit 1
fi

if ! systemctl reload nginx; then
  restore_files
  docker compose -f "$compose_file" up -d --force-recreate || true
  nginx -t -q && systemctl reload nginx || true
  echo "Nginx reload failed; configuration restored" >&2
  exit 1
fi

echo "Reality and loopback WebSocket fallback configuration applied"
echo "Public Reality values are stored in $reality_file"
echo "The fallback path is stored only in $fallback_file"
