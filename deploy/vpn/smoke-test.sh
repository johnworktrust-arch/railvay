#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /root/ceavpn-admin.env
# shellcheck disable=SC1091
source /root/ceavpn-reality.env

api="http://127.0.0.1:8000"
xray="/var/lib/marzban/xray-core/xray"
username="cea_smoke_$(date -u +%s)"
uuid="$(cat /proc/sys/kernel/random/uuid)"
expire="$(date -u -d '+1 day' +%s)"
response="/root/ceavpn-smoke-response.json"
subscription="/root/ceavpn-smoke-subscription.txt"
client_config="/root/ceavpn-smoke-client.json"
client_log="/root/ceavpn-smoke-client.log"
client_pid=""
token=""

cleanup() {
  if [[ -n "$client_pid" ]]; then
    kill "$client_pid" 2>/dev/null || true
    wait "$client_pid" 2>/dev/null || true
  fi
  if [[ -n "$token" ]]; then
    curl -fsS -X DELETE \
      -H "Authorization: Bearer $token" \
      "$api/api/user/$username" >/dev/null 2>&1 || true
  fi
  rm -f "$response" "$subscription" "$client_config" \
    /root/ceavpn-smoke-egress.txt
}
trap cleanup EXIT

token="$(curl -fsS -X POST "$api/api/admin/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$MARZBAN_BOT_USERNAME" \
  --data-urlencode "password=$MARZBAN_BOT_PASSWORD" | jq -er '.access_token')"

jq -n \
  --arg username "$username" \
  --arg uuid "$uuid" \
  --argjson expire "$expire" \
  '{
    username: $username,
    proxies: {vless: {id: $uuid, flow: "xtls-rprx-vision"}},
    inbounds: {vless: ["VLESS TCP REALITY"]},
    expire: $expire,
    data_limit: 104857600,
    data_limit_reset_strategy: "no_reset",
    status: "active",
    note: "automated smoke test"
  }' | curl -fsS -X POST "$api/api/user" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' \
    --data-binary @- > "$response"
chmod 0600 "$response"

jq -e --arg username "$username" \
  '.username == $username and .status == "active" and (.links | length) > 0' \
  "$response" >/dev/null
subscription_url="$(jq -er '.subscription_url' "$response")"
curl -fsS "$subscription_url" -o "$subscription"
[[ -s "$subscription" ]]

jq -n \
  --arg uuid "$uuid" \
  --arg password "$REALITY_PUBLIC_KEY" \
  --arg short_id "$REALITY_SHORT_ID" \
  '{
    log: {loglevel: "warning"},
    inbounds: [{
      listen: "127.0.0.1",
      port: 19080,
      protocol: "socks",
      settings: {auth: "noauth", udp: false}
    }],
    outbounds: [{
      tag: "CEAVPN",
      protocol: "vless",
      settings: {vnext: [{
        address: "79.137.197.51",
        port: 443,
        users: [{id: $uuid, encryption: "none", flow: "xtls-rprx-vision"}]
      }]},
      streamSettings: {
        network: "tcp",
        security: "reality",
        realitySettings: {
          serverName: "cover.79-137-197-51.sslip.io",
          fingerprint: "chrome",
          password: $password,
          shortId: $short_id,
          spiderX: "/"
        }
      }
    }]
  }' > "$client_config"
chmod 0600 "$client_config"

"$xray" run -c "$client_config" > "$client_log" 2>&1 &
client_pid="$!"
for _ in {1..20}; do
  if curl -fsS --max-time 10 \
    --socks5-hostname 127.0.0.1:19080 \
    https://api.ipify.org -o /root/ceavpn-smoke-egress.txt 2>/dev/null; then
    break
  fi
  sleep 1
done

egress="$(cat /root/ceavpn-smoke-egress.txt 2>/dev/null || true)"
if [[ "$egress" != "79.137.197.51" ]]; then
  echo "VPN smoke test failed; egress=$egress" >&2
  tail -50 "$client_log" >&2 || true
  exit 1
fi

echo "VPN smoke test passed"
echo "subscription endpoint: OK"
echo "Reality egress: $egress"
