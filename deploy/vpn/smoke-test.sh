#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

umask 077

# shellcheck disable=SC1091
source /root/ceavpn-admin.env

: "${MARZBAN_BOT_USERNAME:?MARZBAN_BOT_USERNAME is required}"
: "${MARZBAN_BOT_PASSWORD:?MARZBAN_BOT_PASSWORD is required}"

api="http://127.0.0.1:8000"
xray="/var/lib/marzban/xray-core/xray"
server_config="/var/lib/marzban/xray_config.json"
subscription_base_url="${VPN_SMOKE_SUBSCRIPTION_BASE_URL:-https://sub.79-137-197-51.sslip.io:8443}"

for command in curl flock jq python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "missing required command: $command" >&2
    exit 1
  fi
done
for path in "$xray" "$server_config"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done

exec 9>/run/lock/ceavpn-smoke.lock
if ! flock -n 9; then
  echo "another VPN smoke test is already running" >&2
  exit 1
fi

workdir="$(mktemp -d /root/ceavpn-smoke.XXXXXX)"
response="$workdir/create-response.json"
create_payload="$workdir/create-payload.json"
subscription_headers="$workdir/subscription-headers.txt"
subscription_body="$workdir/subscription-body.txt"
manifest="$workdir/manifest.json"
parser_error="$workdir/parser-error.log"
run_id="$(tr -d '-' </proc/sys/kernel/random/uuid)"
username="cea_smoke_${run_id:0:20}"
uuid="$(cat /proc/sys/kernel/random/uuid)"
expire="$(date -u -d '+15 minutes' +%s)"
token=""
client_pid=""
user_cleanup_required=0

get_token() {
  curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 5 --max-time 15 \
    -X POST "$api/api/admin/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "username=$MARZBAN_BOT_USERNAME" \
    --data-urlencode "password=$MARZBAN_BOT_PASSWORD" |
    jq -er '.access_token | select(type == "string" and length > 0)'
}

stop_client() {
  local pid="$client_pid"
  local attempt

  client_pid=""
  if [[ -z "$pid" ]]; then
    return
  fi

  kill "$pid" 2>/dev/null || true
  for ((attempt = 0; attempt < 20; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local original_status=$?
  local cleanup_failed=0
  local cleanup_token=""
  local delete_code=""
  local verify_code=""
  local curl_status=0
  local attempt

  trap - EXIT INT TERM HUP
  set +e
  stop_client

  if ((user_cleanup_required)); then
    cleanup_token="$(get_token 2>/dev/null)"
    if [[ -z "$cleanup_token" ]]; then
      cleanup_token="$token"
    fi

    if [[ -z "$cleanup_token" ]]; then
      cleanup_failed=1
    else
      delete_code="$(curl --noproxy '*' --silent --show-error \
        --connect-timeout 5 --max-time 15 \
        -o "$workdir/delete-response.json" -w '%{http_code}' \
        -X DELETE -H "Authorization: Bearer $cleanup_token" \
        "$api/api/user/$username")"
      curl_status=$?
      if ((curl_status != 0)) ||
        [[ "$delete_code" != "200" && "$delete_code" != "204" && "$delete_code" != "404" ]]; then
        cleanup_failed=1
      fi

      verify_code=""
      for ((attempt = 0; attempt < 5; attempt++)); do
        verify_code="$(curl --noproxy '*' --silent --show-error \
          --connect-timeout 5 --max-time 15 \
          -o "$workdir/delete-verify-response.json" -w '%{http_code}' \
          -H "Authorization: Bearer $cleanup_token" \
          "$api/api/user/$username")"
        curl_status=$?
        if ((curl_status == 0)) && [[ "$verify_code" == "404" ]]; then
          break
        fi
        sleep 1
      done
      if ((curl_status != 0)) || [[ "$verify_code" != "404" ]]; then
        cleanup_failed=1
      fi
    fi
  fi

  if [[ "$workdir" == /root/ceavpn-smoke.* && -d "$workdir" ]]; then
    rm -rf -- "$workdir"
  else
    cleanup_failed=1
  fi
  if ((cleanup_failed)); then
    echo "VPN smoke test cleanup failed: test user removal was not verified" >&2
    if ((original_status == 0)); then
      original_status=1
    fi
  fi
  exit "$original_status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

is_ipv4() {
  python3 - "$1" <<'PY' >/dev/null 2>&1
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1].strip())
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if address.version == 4 else 1)
PY
}

fetch_direct_ipv4() {
  local url
  local value

  for url in https://api4.ipify.org https://v4.ident.me; do
    if value="$(curl --noproxy '*' --fail --silent --show-error \
      --connect-timeout 5 --max-time 10 "$url" 2>/dev/null)" &&
      is_ipv4 "$value"; then
      printf '%s\n' "$value"
      return 0
    fi
  done
  return 1
}

fetch_socks_ipv4() {
  local port="$1"
  local url
  local value

  # The hostname is deliberately passed to the SOCKS server. A successful
  # request therefore covers DNS resolution through the VLESS connection too.
  for url in https://api4.ipify.org https://v4.ident.me; do
    if value="$(curl --fail --silent --show-error \
      --connect-timeout 5 --max-time 12 \
      --proxy "socks5h://127.0.0.1:$port" "$url" 2>/dev/null)" &&
      is_ipv4 "$value"; then
      printf '%s\n' "$value"
      return 0
    fi
  done
  return 1
}

choose_port() {
  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

if ! token="$(get_token)"; then
  echo "VPN smoke test failed: Marzban authentication failed" >&2
  exit 1
fi

if ! inbound_tags="$(jq -cer '
  [.inbounds[]?
    | select(.protocol == "vless")
    | .tag
    | select(type == "string" and length > 0)]
  | unique
  | select(length > 0)
' "$server_config")"; then
  echo "VPN smoke test failed: no active VLESS inbound tags" >&2
  exit 1
fi
if ! jq -e '
  . == ["VLESS TCP REALITY", "VLESS WS TLS FALLBACK"]
' <<<"$inbound_tags" >/dev/null; then
  echo "VPN smoke test failed: unexpected active VLESS inbound tags" >&2
  exit 1
fi
# Both transports remain configured on the one VPS for rollback safety, but
# only the Happ-compatible WS/TLS host is published to client subscriptions.
expected_vless_profiles=1

jq -n \
  --arg username "$username" \
  --arg uuid "$uuid" \
  --argjson expire "$expire" \
  --argjson inbound_tags "$inbound_tags" \
  '{
    username: $username,
    proxies: {vless: {id: $uuid, flow: "xtls-rprx-vision"}},
    inbounds: {vless: $inbound_tags},
    expire: $expire,
    data_limit: 104857600,
    data_limit_reset_strategy: "no_reset",
    status: "active",
    note: "automated end-to-end smoke test"
  }' >"$create_payload"

# From this point cleanup attempts the DELETE even if the create request loses
# its response after Marzban has committed the user.
user_cleanup_required=1
if ! create_code="$(curl --noproxy '*' --silent --show-error \
  --connect-timeout 5 --max-time 20 \
  -o "$response" -w '%{http_code}' \
  -X POST "$api/api/user" \
  -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' \
  --data-binary "@$create_payload")"; then
  echo "VPN smoke test failed: could not create test user" >&2
  exit 1
fi
if [[ "$create_code" != "200" && "$create_code" != "201" ]]; then
  echo "VPN smoke test failed: Marzban rejected test user creation" >&2
  exit 1
fi
if ! jq -e --arg username "$username" --arg uuid "$uuid" '
  .username == $username and
  .status == "active" and
  .proxies.vless.id == $uuid and
  (.links | type == "array") and
  (.links | length > 0) and
  (.subscription_url | type == "string" and length > 0)
' "$response" >/dev/null; then
  echo "VPN smoke test failed: invalid Marzban create response" >&2
  exit 1
fi

if ! subscription_url="$(python3 - "$response" "$subscription_base_url" <<'PY'
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

with open(sys.argv[1], encoding="utf-8") as source:
    raw = json.load(source).get("subscription_url")
base = urlsplit(sys.argv[2])
if not isinstance(raw, str) or not raw:
    raise SystemExit(1)
if base.scheme != "https" or not base.hostname or base.username or base.password:
    raise SystemExit(1)

absolute = urljoin(sys.argv[2].rstrip("/") + "/", raw)
target = urlsplit(absolute)
try:
    base_port = base.port or 443
    target_port = target.port or 443
except ValueError:
    raise SystemExit(1)

if (
    target.scheme != "https"
    or target.hostname != base.hostname
    or target_port != base_port
    or target.username
    or target.password
    or target.query
    or target.fragment
    or not re.fullmatch(r"/sub/[A-Za-z0-9._~-]{1,160}/?", target.path)
):
    raise SystemExit(1)
print(absolute)
PY
)"; then
  echo "VPN smoke test failed: untrusted subscription URL" >&2
  exit 1
fi

if ! subscription_code="$(curl --noproxy '*' --silent --show-error \
  --connect-timeout 5 --max-time 20 --max-redirs 0 \
  --proto '=https' --max-filesize 262144 \
  -H 'Accept: text/plain' -H 'User-Agent: CEA-VPN-Smoke/1.0' \
  -D "$subscription_headers" -o "$subscription_body" \
  -w '%{http_code}' "$subscription_url")"; then
  echo "VPN smoke test failed: subscription fetch failed" >&2
  exit 1
fi
if [[ "$subscription_code" != "200" ]]; then
  echo "VPN smoke test failed: subscription endpoint did not return HTTP 200" >&2
  exit 1
fi
if [[ ! -s "$subscription_body" ]]; then
  echo "VPN smoke test failed: subscription body is empty" >&2
  exit 1
fi

if ! python3 - \
  "$subscription_headers" "$subscription_body" "$response" \
  "$uuid" "$workdir" "$manifest" "$expected_vless_profiles" \
  2>"$parser_error" <<'PY'
import base64
import binascii
import ipaddress
import json
import os
import re
import sys
import uuid as uuid_module
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

(
    headers_path,
    body_path,
    response_path,
    expected_uuid,
    output_dir,
    manifest_path,
    expected_profile_count_text,
) = sys.argv[1:]
expected_profile_count = int(expected_profile_count_text)


class ValidationError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ValidationError(code)


def final_headers(path):
    raw = Path(path).read_bytes()
    blocks = [block for block in re.split(rb"\r?\n\r?\n", raw) if block.startswith(b"HTTP/")]
    require(blocks, "missing_http_headers")
    result = {}
    for line in re.split(rb"\r?\n", blocks[-1])[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        try:
            name = key.decode("ascii").strip().lower()
            text = value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise ValidationError("invalid_http_headers") from exc
        result.setdefault(name, []).append(text)
    return result


def decode_subscription(path):
    raw = Path(path).read_bytes()
    require(0 < len(raw) <= 262144, "invalid_subscription_size")
    try:
        direct = raw.decode("utf-8")
    except UnicodeDecodeError:
        direct = ""

    if any(line.strip().lower().startswith("vless://") for line in direct.splitlines()):
        decoded = direct
    else:
        compact = b"".join(raw.split())
        try:
            decoded_bytes = base64.b64decode(compact, validate=True)
            decoded = decoded_bytes.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValidationError("invalid_subscription_encoding") from exc

    require(len(decoded.encode("utf-8")) <= 524288, "decoded_subscription_too_large")
    require("\x00" not in decoded, "invalid_subscription_text")
    return [line.strip() for line in decoded.splitlines() if line.strip()]


def parse_uuid_from_uri(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "vless" or not parsed.username:
            return None
        return str(uuid_module.UUID(unquote(parsed.username)))
    except (ValueError, AttributeError):
        return None


def parse_vless(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("invalid_vless_authority") from exc

    require(parsed.scheme.lower() == "vless", "invalid_vless_scheme")
    require(parsed.username is not None and parsed.password is None, "invalid_vless_credentials")
    try:
        parsed_uuid = str(uuid_module.UUID(unquote(parsed.username)))
    except ValueError as exc:
        raise ValidationError("invalid_vless_uuid") from exc
    require(parsed_uuid == expected_uuid, "unexpected_vless_uuid")
    require(parsed.hostname is not None and port is not None and 1 <= port <= 65535, "invalid_vless_endpoint")
    host = parsed.hostname
    require(not any(character.isspace() or ord(character) < 32 for character in host), "invalid_vless_host")
    require("%" not in host, "unsupported_scoped_ipv6")

    require(re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query) is None, "invalid_vless_escape")
    parameters = {}
    for key, value_part in parse_qsl(
        parsed.query, keep_blank_values=True, strict_parsing=True
    ):
        require(key not in parameters, "duplicate_vless_parameter")
        parameters[key] = value_part

    network = parameters.get("type", "")
    security = parameters.get("security", "")
    encryption = parameters.get("encryption", "none") or "none"
    require(encryption == "none", "unsupported_vless_encryption")

    user = {"id": parsed_uuid, "encryption": "none"}
    stream = {"network": network, "security": security}

    if security == "reality" and network in ("tcp", "raw"):
        allowed = {
            "security", "type", "headerType", "flow", "sni", "fp",
            "pbk", "sid", "spx", "path", "host", "encryption",
        }
        require(not (set(parameters) - allowed), "unsupported_reality_parameter")
        require(parameters.get("headerType", "none") in ("", "none"), "unsupported_reality_header")
        require(parameters.get("flow") == "xtls-rprx-vision", "invalid_reality_flow")
        for key in ("sni", "fp", "pbk", "sid"):
            require(bool(parameters.get(key)), f"missing_reality_{key}")
        require(re.fullmatch(r"[A-Za-z0-9_-]{32,64}", parameters["pbk"]) is not None, "invalid_reality_pbk")
        require(re.fullmatch(r"(?:[0-9A-Fa-f]{2}){1,8}", parameters["sid"]) is not None, "invalid_reality_sid")
        user["flow"] = parameters["flow"]
        stream["realitySettings"] = {
            "serverName": parameters["sni"],
            "fingerprint": parameters["fp"],
            # Current pinned Xray uses `password` for the client-side Reality
            # public key. Its value still comes only from this subscription URI.
            "password": parameters["pbk"],
            "shortId": parameters["sid"],
            "spiderX": parameters.get("spx") or "/",
        }
        kind = "reality"
    elif security == "tls" and network == "ws":
        allowed = {
            "security", "type", "headerType", "path", "host", "sni",
            "fp", "alpn", "allowInsecure", "encryption",
        }
        require(not (set(parameters) - allowed), "unsupported_ws_tls_parameter")
        require(parameters.get("headerType", "none") in ("", "none"), "unsupported_ws_tls_header")
        require(parameters.get("allowInsecure", "0").lower() in ("", "0", "false"), "insecure_ws_tls_uri")
        server_name = parameters.get("sni") or parameters.get("host")
        require(bool(server_name), "missing_ws_tls_server_name")
        tls_settings = {"serverName": server_name, "allowInsecure": False}
        if parameters.get("fp"):
            tls_settings["fingerprint"] = parameters["fp"]
        if parameters.get("alpn"):
            alpn = [item.strip() for item in parameters["alpn"].split(",") if item.strip()]
            require(alpn, "invalid_ws_tls_alpn")
            tls_settings["alpn"] = alpn
        ws_settings = {"path": parameters.get("path") or "/"}
        if parameters.get("host"):
            ws_settings["headers"] = {"Host": parameters["host"]}
        stream["tlsSettings"] = tls_settings
        stream["wsSettings"] = ws_settings
        kind = "ws-tls"
    else:
        raise ValidationError("unsupported_vless_profile")

    try:
        endpoint_family = f"ipv{ipaddress.ip_address(host).version}"
    except ValueError:
        endpoint_family = "dns"

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": 0,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [{
            "tag": "CEAVPN-SMOKE",
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": host,
                "port": port,
                "users": [user],
            }]},
            "streamSettings": stream,
        }],
    }
    return config, {
        "kind": kind,
        "remark": unquote(parsed.fragment),
        "endpoint_family": endpoint_family,
        "endpoint_port": port,
    }


try:
    headers = final_headers(headers_path)
    content_types = headers.get("content-type", [])
    require(content_types, "missing_content_type")
    require(content_types[-1].split(";", 1)[0].strip().lower() == "text/plain", "invalid_content_type")
    routing_values = headers.get("routing-enable", [])
    require(routing_values and all(value.strip() == "0" for value in routing_values), "invalid_routing_enable")

    lines = decode_subscription(body_path)
    with open(response_path, encoding="utf-8") as source:
        api_response = json.load(source)
    api_links = api_response.get("links")
    require(isinstance(api_links, list) and all(isinstance(item, str) for item in api_links), "invalid_api_links")

    body_vless_links = [line for line in lines if line[:8].lower() == "vless://"]
    api_vless_links = [line for line in api_links if line[:8].lower() == "vless://"]
    require(body_vless_links, "no_vless_uri")
    require(
        all(parse_uuid_from_uri(line) == expected_uuid for line in body_vless_links),
        "unexpected_subscription_vless_uri",
    )
    require(
        all(parse_uuid_from_uri(line) == expected_uuid for line in api_vless_links),
        "unexpected_api_vless_uri",
    )
    body_links = body_vless_links
    api_user_links = api_vless_links
    require(body_links, "no_active_vless_uri")
    require(len(body_links) <= 8, "too_many_vless_profiles")
    require(len(body_links) == len(set(body_links)), "duplicate_subscription_uri")
    require(
        len(body_links) == len(api_user_links),
        "subscription_api_link_count_mismatch",
    )

    profiles = []
    canonical_body_profiles = []
    output = Path(output_dir)
    for index, link in enumerate(body_links):
        config, metadata = parse_vless(link)
        canonical_body_profiles.append(
            json.dumps(config["outbounds"][0], sort_keys=True, separators=(",", ":"))
        )
        filename = f"client-{index}.json"
        destination = output / filename
        destination.write_text(json.dumps(config, separators=(",", ":")), encoding="utf-8")
        os.chmod(destination, 0o600)
        metadata["file"] = filename
        profiles.append(metadata)

    canonical_api_profiles = []
    for link in api_user_links:
        config, _ = parse_vless(link)
        canonical_api_profiles.append(
            json.dumps(config["outbounds"][0], sort_keys=True, separators=(",", ":"))
        )
    require(
        sorted(canonical_body_profiles) == sorted(canonical_api_profiles),
        "subscription_api_profile_mismatch",
    )

    require(profiles, "no_supported_vless_profiles")
    require(len(profiles) == expected_profile_count, "missing_vless_profile")
    kinds = sorted(profile["kind"] for profile in profiles)
    require(kinds == ["ws-tls"], "invalid_public_vless_profile")
    require(
        profiles[0]["remark"] == "🇳🇱 Нидерланды · Амстердам",
        "invalid_public_profile_name",
    )
    Path(manifest_path).write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
except (ValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
    if isinstance(exc, ValidationError):
        print(f"ValidationError:{exc}", file=sys.stderr)
    else:
        print(type(exc).__name__, file=sys.stderr)
    raise SystemExit(1)
PY
then
  parser_diagnostic="$(head -n 1 "$parser_error" 2>/dev/null || true)"
  if [[ "$parser_diagnostic" =~ ^(ValidationError:[a-z0-9_]+|OSError|ValueError|JSONDecodeError)$ ]]; then
    echo "VPN subscription validation detail: $parser_diagnostic" >&2
  fi
  echo "VPN smoke test failed: subscription headers or active VLESS URI are invalid" >&2
  exit 1
fi

if ! expected_egress_ipv4="${VPN_SMOKE_EXPECTED_EGRESS_IPV4:-$(fetch_direct_ipv4)}" ||
  ! is_ipv4 "$expected_egress_ipv4"; then
  echo "VPN smoke test failed: could not determine expected IPv4 egress" >&2
  exit 1
fi

profile_count="$(jq -er '.profiles | length | select(. > 0)' "$manifest")"
for ((profile_index = 0; profile_index < profile_count; profile_index++)); do
  profile_file="$(jq -er --argjson index "$profile_index" '.profiles[$index].file' "$manifest")"
  profile_kind="$(jq -er --argjson index "$profile_index" '.profiles[$index].kind' "$manifest")"
  if [[ ! "$profile_file" =~ ^client-[0-9]+\.json$ ]] ||
    [[ "$profile_kind" != "reality" && "$profile_kind" != "ws-tls" ]]; then
    echo "VPN smoke test failed: unsafe profile manifest" >&2
    exit 1
  fi

  client_config="$workdir/$profile_file"
  client_log="$workdir/${profile_file%.json}.log"
  validation_log="$workdir/${profile_file%.json}-validation.log"
  socks_port="$(choose_port)"
  patched_config="$workdir/${profile_file%.json}-ready.json"
  jq --argjson port "$socks_port" '.inbounds[0].port = $port' \
    "$client_config" >"$patched_config"
  mv "$patched_config" "$client_config"

  if ! "$xray" run -test -c "$client_config" >"$validation_log" 2>&1; then
    echo "VPN smoke test failed: Xray rejected $profile_kind profile from subscription" >&2
    exit 1
  fi

  "$xray" run -c "$client_config" >"$client_log" 2>&1 &
  client_pid="$!"
  egress=""
  for ((attempt = 0; attempt < 5; attempt++)); do
    if ! kill -0 "$client_pid" 2>/dev/null; then
      break
    fi
    if egress="$(fetch_socks_ipv4 "$socks_port")"; then
      break
    fi
    sleep 1
  done

  if ! is_ipv4 "$egress" || [[ "$egress" != "$expected_egress_ipv4" ]]; then
    echo "VPN smoke test failed: $profile_kind SOCKS DNS/IPv4 egress check failed" >&2
    exit 1
  fi
  stop_client
  echo "$profile_kind URI: PASS"
done

echo "VPN end-to-end smoke test passed"
echo "subscription headers and active URI set: PASS"
echo "tested VLESS profiles: $profile_count"
echo "SOCKS DNS and IPv4 egress: PASS"
