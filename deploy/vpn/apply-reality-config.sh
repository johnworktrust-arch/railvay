#!/usr/bin/env bash
set -euo pipefail

xray_template="${1:-/opt/marzban/xray_config.template.json}"
nginx_template="${2:-/opt/marzban/nginx.template.conf}"
nginx_output="${3:-/etc/nginx/sites-enabled/ceavpn}"
key_file="/root/ceavpn-reality-keys.txt"
reality_file="/root/ceavpn-reality.env"
fallback_file="/root/ceavpn-fallback.env"
xhttp_file="/root/ceavpn-xhttp.env"
data_dir="/var/lib/marzban"
xray_output="$data_dir/xray_config.json"
compose_file="/opt/marzban/docker-compose.yml"
xray_new="$data_dir/xray_config.new.json"
nginx_new="${nginx_output}.new"
backup_dir="/root/ceavpn-config-backups"
nginx_test_config=""
reality_new=""
fallback_new=""
xhttp_new=""
node_file="/root/ceavpn-node.env"
lte_exit_file="/root/ceavpn-lte-exit.env"

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

cleanup() {
  status=$?
  set +e
  unset CEAVPN_REALITY_PRIVATE_KEY CEAVPN_REALITY_PUBLIC_KEY
  unset CEAVPN_REALITY_SHORT_ID CEAVPN_FALLBACK_WS_PATH CEAVPN_XHTTP_PATH
  unset CEAVPN_XRAY_TEMPLATE CEAVPN_XRAY_OUTPUT
  unset CEAVPN_NGINX_TEMPLATE CEAVPN_NGINX_OUTPUT
  unset CEAVPN_SUB_DOMAIN CEAVPN_COVER_DOMAIN
  unset CEAVPN_NODE_MODE CEAVPN_SERVER_CODE
  unset CEAVPN_LTE_EXIT_ADDRESS CEAVPN_LTE_EXIT_PORT
  unset CEAVPN_LTE_EXIT_UUID CEAVPN_LTE_EXIT_SNI CEAVPN_LTE_EXIT_HOST
  unset CEAVPN_LTE_EXIT_PATH
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
  if [[ -n "$xhttp_new" ]]; then
    rm -f -- "$xhttp_new"
  fi
  exit "$status"
}
trap cleanup EXIT

for path in \
  "$xray_template" \
  "$nginx_template" \
  "$key_file" \
  "$node_file" \
  "$compose_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done

for command in openssl python3 nginx docker systemctl curl; do
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
install -d -o root -g root -m 0755 /etc/nginx/snippets
if [[ ! -e /etc/nginx/snippets/ceavpn-relays.conf ]]; then
  install -o root -g root -m 0644 /dev/null \
    /etc/nginx/snippets/ceavpn-relays.conf
fi

private_key="$(
  sed -n \
    -e 's/^Private key:[[:space:]]*//p' \
    -e 's/^PrivateKey:[[:space:]]*//p' \
    "$key_file"
)"
public_key="$(
  sed -n \
    -e 's/^Public key:[[:space:]]*//p' \
    -e 's/^Password (PublicKey):[[:space:]]*//p' \
    "$key_file"
)"
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
# shellcheck disable=SC1090
source "$node_file"

: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
: "${CEAVPN_COVER_DOMAIN:?CEAVPN_COVER_DOMAIN is required}"

node_mode="${CEAVPN_NODE_MODE:-direct}"
if [[ "$node_mode" == "lte" || "$node_mode" == "whitelist" ]]; then
  if [[ ! -s "$lte_exit_file" ]]; then
    echo "missing required file: $lte_exit_file" >&2
    exit 1
  fi
  chmod 0600 "$lte_exit_file"
  # shellcheck disable=SC1090
  source "$lte_exit_file"
  : "${CEAVPN_LTE_EXIT_ADDRESS:?CEAVPN_LTE_EXIT_ADDRESS is required}"
  : "${CEAVPN_LTE_EXIT_PORT:?CEAVPN_LTE_EXIT_PORT is required}"
  : "${CEAVPN_LTE_EXIT_UUID:?CEAVPN_LTE_EXIT_UUID is required}"
  : "${CEAVPN_LTE_EXIT_SNI:?CEAVPN_LTE_EXIT_SNI is required}"
  : "${CEAVPN_LTE_EXIT_HOST:?CEAVPN_LTE_EXIT_HOST is required}"
  : "${CEAVPN_LTE_EXIT_PATH:?CEAVPN_LTE_EXIT_PATH is required}"
elif [[ "$node_mode" != "direct" ]]; then
  echo "CEAVPN_NODE_MODE must be direct, lte, or whitelist" >&2
  exit 1
fi

if [[ "$node_mode" == "whitelist" ]]; then
  : "${CEAVPN_SERVER_CODE:?CEAVPN_SERVER_CODE is required}"
  if [[ ! "$CEAVPN_SERVER_CODE" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
    echo "invalid CEAVPN_SERVER_CODE" >&2
    exit 1
  fi
  if [[ ! -e "$xhttp_file" ]]; then
    xhttp_path="/xhttp-$(openssl rand -hex 24)"
    xhttp_new="$(mktemp "${xhttp_file}.new.XXXXXX")"
    printf 'XHTTP_PATH=%s\n' "$xhttp_path" > "$xhttp_new"
    chmod 0600 "$xhttp_new"
    mv "$xhttp_new" "$xhttp_file"
    xhttp_new=""
  elif [[ ! -s "$xhttp_file" ]]; then
    echo "XHTTP environment file is empty" >&2
    exit 1
  fi
  chmod 0600 "$xhttp_file"
  # shellcheck disable=SC1090
  source "$xhttp_file"
  : "${XHTTP_PATH:?XHTTP_PATH is required}"
  if [[ ! "$XHTTP_PATH" =~ ^/xhttp-[0-9a-f]{48}$ ]]; then
    echo "invalid XHTTP path" >&2
    exit 1
  fi
fi

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
export CEAVPN_SUB_DOMAIN CEAVPN_COVER_DOMAIN
export CEAVPN_NODE_MODE="$node_mode"
if [[ "$node_mode" == "lte" || "$node_mode" == "whitelist" ]]; then
  export CEAVPN_LTE_EXIT_ADDRESS CEAVPN_LTE_EXIT_PORT
  export CEAVPN_LTE_EXIT_UUID CEAVPN_LTE_EXIT_SNI
  export CEAVPN_LTE_EXIT_HOST CEAVPN_LTE_EXIT_PATH
fi
if [[ "$node_mode" == "whitelist" ]]; then
  export CEAVPN_XHTTP_PATH="$XHTTP_PATH"
fi

python3 - <<'PY'
import json
import os
import re
import uuid
from pathlib import Path

xray_template = Path(os.environ["CEAVPN_XRAY_TEMPLATE"]).read_text(
    encoding="utf-8"
)
nginx_template = Path(os.environ["CEAVPN_NGINX_TEMPLATE"]).read_text(
    encoding="utf-8"
)

xray_replacements = {
    "__REALITY_PRIVATE_KEY__": os.environ["CEAVPN_REALITY_PRIVATE_KEY"],
    "__REALITY_SHORT_ID__": os.environ["CEAVPN_REALITY_SHORT_ID"],
    "__COVER_DOMAIN__": os.environ["CEAVPN_COVER_DOMAIN"],
}
node_mode = os.environ["CEAVPN_NODE_MODE"]
if node_mode == "whitelist":
    if os.environ["CEAVPN_COVER_DOMAIN"] == os.environ["CEAVPN_SUB_DOMAIN"]:
        raise SystemExit(
            "whitelist REALITY camouflage must be a remote domain"
        )
    xray_replacements["__XHTTP_PATH__"] = os.environ["CEAVPN_XHTTP_PATH"]
    xray_replacements["__REALITY_TARGET__"] = (
        f"{os.environ['CEAVPN_COVER_DOMAIN']}:443"
    )
else:
    xray_replacements.update(
        {
            "__REALITY_PUBLIC_KEY__": os.environ[
                "CEAVPN_REALITY_PUBLIC_KEY"
            ],
            "__FALLBACK_WS_PATH__": os.environ["CEAVPN_FALLBACK_WS_PATH"],
        }
    )
if node_mode in {"lte", "whitelist"}:
    try:
        exit_port = int(os.environ["CEAVPN_LTE_EXIT_PORT"])
    except ValueError as exc:
        raise SystemExit("invalid LTE exit port") from exc
    if not 1 <= exit_port <= 65535:
        raise SystemExit("invalid LTE exit port")
    try:
        exit_uuid = str(uuid.UUID(os.environ["CEAVPN_LTE_EXIT_UUID"]))
    except ValueError as exc:
        raise SystemExit("invalid LTE exit UUID") from exc
    exit_path = os.environ["CEAVPN_LTE_EXIT_PATH"]
    if (
        not exit_path.startswith("/ws-")
        or len(exit_path) != 52
        or any(character not in "0123456789abcdef" for character in exit_path[4:])
    ):
        raise SystemExit("invalid LTE exit WebSocket path")
    for variable in (
        "CEAVPN_LTE_EXIT_ADDRESS",
        "CEAVPN_LTE_EXIT_SNI",
        "CEAVPN_LTE_EXIT_HOST",
    ):
        value = os.environ[variable]
        if (
            not value
            or len(value) > 253
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise SystemExit(f"invalid LTE exit endpoint: {variable}")
    xray_replacements.update(
        {
            "__LTE_EXIT_ADDRESS__": os.environ["CEAVPN_LTE_EXIT_ADDRESS"],
            "__LTE_EXIT_PORT__": str(exit_port),
            "__LTE_EXIT_UUID__": exit_uuid,
            "__LTE_EXIT_SNI__": os.environ["CEAVPN_LTE_EXIT_SNI"],
            "__LTE_EXIT_HOST__": os.environ["CEAVPN_LTE_EXIT_HOST"],
            "__LTE_EXIT_PATH__": exit_path,
        }
    )
nginx_replacements = {
    "__FALLBACK_WS_PATH__": os.environ["CEAVPN_FALLBACK_WS_PATH"],
    "__SUB_DOMAIN__": os.environ["CEAVPN_SUB_DOMAIN"],
    "__COVER_DOMAIN__": os.environ["CEAVPN_COVER_DOMAIN"],
    "__HTTP_SERVER_NAMES__": (
        os.environ["CEAVPN_SUB_DOMAIN"]
        if node_mode == "whitelist"
        else (
            f"{os.environ['CEAVPN_SUB_DOMAIN']} "
            f"{os.environ['CEAVPN_COVER_DOMAIN']}"
        )
    ),
    "__WHITELIST_PROBE_LOCATION__": (
        "location = /.well-known/ceavpn-whitelist-probe {\n"
        "        access_log off;\n"
        "        default_type application/json;\n"
        "        add_header Cache-Control \"no-store\" always;\n"
        "        return 200 '{\"service\":\"ceavpn\",\"status\":\"candidate\"}';\n"
        "    }\n\n"
        "    location = /.well-known/ceavpn-whitelist-status {\n"
        "        access_log off;\n"
        "        default_type application/json;\n"
        "        add_header Cache-Control \"no-store\" always;\n"
        "        add_header X-Content-Type-Options \"nosniff\" always;\n"
        "        alias /var/www/ceavpn-whitelist/status.json;\n"
        "    }"
        if node_mode == "whitelist"
        else ""
    ),
}

for placeholder in xray_replacements:
    if xray_template.count(placeholder) != 1:
        raise SystemExit(f"invalid Xray placeholder count: {placeholder}")
for placeholder in nginx_replacements:
    if nginx_template.count(placeholder) < 1:
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
if node_mode == "whitelist":
    xhttp = inbounds.get("VLESS XHTTP REALITY", {})
    stream = xhttp.get("streamSettings", {})
    reality = stream.get("realitySettings", {})
    if (
        set(inbounds) != {"VLESS XHTTP REALITY"}
        or xhttp.get("listen") != "0.0.0.0"
        or xhttp.get("port") != 443
        or xhttp.get("protocol") != "vless"
        or xhttp.get("settings", {}).get("clients") != []
        or xhttp.get("settings", {}).get("decryption") != "none"
        or stream.get("network") != "xhttp"
        or stream.get("security") != "reality"
        or stream.get("xhttpSettings", {}).get("path")
        != os.environ["CEAVPN_XHTTP_PATH"]
        or set(reality) != {
            "show",
            "target",
            "xver",
            "serverNames",
            "privateKey",
            "shortIds",
        }
        or reality.get("target")
        != f"{os.environ['CEAVPN_COVER_DOMAIN']}:443"
        or reality.get("serverNames") != [os.environ["CEAVPN_COVER_DOMAIN"]]
    ):
        raise SystemExit("invalid whitelist XHTTP/Reality inbound")
else:
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

outbounds = {
    outbound.get("tag"): outbound
    for outbound in parsed_xray.get("outbounds", [])
    if isinstance(outbound, dict)
}
if node_mode in {"lte", "whitelist"}:
    exit_tag = "LTE EXIT" if node_mode == "lte" else "WHITELIST EXIT"
    lte_exit = outbounds.get(exit_tag, {})
    vnext = lte_exit.get("settings", {}).get("vnext", [])
    if (
        lte_exit.get("protocol") != "vless"
        or len(vnext) != 1
        or vnext[0].get("address") != os.environ["CEAVPN_LTE_EXIT_ADDRESS"]
        or vnext[0].get("port") != int(os.environ["CEAVPN_LTE_EXIT_PORT"])
        or vnext[0].get("users", [{}])[0].get("id")
        != str(uuid.UUID(os.environ["CEAVPN_LTE_EXIT_UUID"]))
        or lte_exit.get("streamSettings", {}).get("network") != "ws"
        or lte_exit.get("streamSettings", {}).get("security") != "tls"
    ):
        raise SystemExit("invalid LTE chained exit")
    routed_inbounds = [
        rule
        for rule in parsed_xray.get("routing", {}).get("rules", [])
        if rule.get("outboundTag") == exit_tag
    ]
    expected_inbound_tags = (
        {"VLESS WS TLS FALLBACK", "VLESS TCP REALITY"}
        if node_mode == "lte"
        else {"VLESS XHTTP REALITY"}
    )
    if (
        len(routed_inbounds) != 1
        or set(routed_inbounds[0].get("inboundTag", []))
        != expected_inbound_tags
    ):
        raise SystemExit("gateway inbounds are not forced through the chained exit")
elif "LTE EXIT" in outbounds or "WHITELIST EXIT" in outbounds:
    raise SystemExit("direct node unexpectedly contains an LTE exit")

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
probe_marker = '{"service":"ceavpn","status":"candidate"}'
if (probe_marker in rendered_nginx) != (node_mode == "whitelist"):
    raise SystemExit("whitelist qualification probe exposure is invalid")
status_location = "location = /.well-known/ceavpn-whitelist-status"
status_alias = "alias /var/www/ceavpn-whitelist/status.json;"
if (
    (status_location in rendered_nginx) != (node_mode == "whitelist")
    or (status_alias in rendered_nginx) != (node_mode == "whitelist")
):
    raise SystemExit("whitelist public status exposure is invalid")

Path(os.environ["CEAVPN_XRAY_OUTPUT"]).write_text(
    rendered_xray, encoding="utf-8"
)
Path(os.environ["CEAVPN_NGINX_OUTPUT"]).write_text(
    rendered_nginx, encoding="utf-8"
)
PY

unset CEAVPN_REALITY_PRIVATE_KEY CEAVPN_REALITY_PUBLIC_KEY
unset CEAVPN_REALITY_SHORT_ID CEAVPN_FALLBACK_WS_PATH CEAVPN_XHTTP_PATH
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

wait_for_runtime() {
  local ready=0
  for _ in {1..60}; do
    if [[ -n "$(
        docker compose -f "$compose_file" ps --status running -q marzban \
          2>/dev/null
      )" ]] &&
      curl --noproxy '*' --fail --silent --show-error \
        --connect-timeout 2 --max-time 3 \
        http://127.0.0.1:8000/dashboard/ -o /dev/null 2>/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  (( ready ))
}

rollback_runtime() {
  local failed=0
  restore_files
  if ! docker compose -f "$compose_file" up -d --force-recreate; then
    failed=1
  fi
  if ! nginx -t -q || ! systemctl reload nginx; then
    failed=1
  fi
  if ! wait_for_runtime; then
    failed=1
  fi
  if (( failed )); then
    echo "configuration rollback readiness failed; manual recovery required" >&2
    return 1
  fi
}

mv "$xray_new" "$xray_output"
mv "$nginx_new" "$nginx_output"

if ! nginx -t -q; then
  rollback_runtime || true
  echo "Nginx validation failed; configuration restored" >&2
  exit 1
fi

if ! docker compose -f "$compose_file" up -d --force-recreate; then
  rollback_runtime || true
  echo "Xray restart failed; configuration restored" >&2
  exit 1
fi

if ! systemctl reload nginx; then
  rollback_runtime || true
  echo "Nginx reload failed; configuration restored" >&2
  exit 1
fi

if ! wait_for_runtime; then
  rollback_runtime || true
  echo "Marzban/Xray readiness failed; configuration restored" >&2
  exit 1
fi

echo "Xray and Nginx configuration applied"
echo "Public Reality values are stored in $reality_file"
if [[ "$node_mode" == "whitelist" ]]; then
  echo "The XHTTP path is stored only in $xhttp_file"
  echo "Whitelist publication remains gated by explicit restricted-SIM qualification"
else
  echo "The fallback path is stored only in $fallback_file"
fi
