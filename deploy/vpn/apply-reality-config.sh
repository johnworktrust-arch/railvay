#!/usr/bin/env bash
set -euo pipefail

template="${1:-/opt/marzban/xray_config.template.json}"
key_file="/root/ceavpn-reality-keys.txt"
public_file="/root/ceavpn-reality.env"
data_dir="/var/lib/marzban"
compose_file="/opt/marzban/docker-compose.yml"

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

for path in "$template" "$key_file" "$compose_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done

private_key="$(sed -n 's/^Private key:[[:space:]]*//p' "$key_file")"
public_key="$(sed -n 's/^Public key:[[:space:]]*//p' "$key_file")"
if [[ -z "$private_key" || -z "$public_key" ]]; then
  echo "could not parse Reality keys" >&2
  exit 1
fi

if [[ ! -s "$public_file" ]]; then
  short_id="$(openssl rand -hex 8)"
  umask 077
  printf 'REALITY_PUBLIC_KEY=%s\nREALITY_SHORT_ID=%s\n' \
    "$public_key" "$short_id" > "$public_file"
fi

# shellcheck disable=SC1090
source "$public_file"
if [[ ! "$REALITY_SHORT_ID" =~ ^[0-9a-f]{16}$ ]]; then
  echo "invalid Reality short ID" >&2
  exit 1
fi

export CEAVPN_REALITY_PRIVATE_KEY="$private_key"
export CEAVPN_REALITY_PUBLIC_KEY="$REALITY_PUBLIC_KEY"
export CEAVPN_REALITY_SHORT_ID="$REALITY_SHORT_ID"
export CEAVPN_TEMPLATE="$template"
export CEAVPN_OUTPUT="$data_dir/xray_config.new.json"
python3 - <<'PY'
import json
import os
from pathlib import Path

template = Path(os.environ["CEAVPN_TEMPLATE"]).read_text(encoding="utf-8")
rendered = template.replace(
    "__REALITY_PRIVATE_KEY__", os.environ["CEAVPN_REALITY_PRIVATE_KEY"]
).replace(
    "__REALITY_PUBLIC_KEY__", os.environ["CEAVPN_REALITY_PUBLIC_KEY"]
).replace("__REALITY_SHORT_ID__", os.environ["CEAVPN_REALITY_SHORT_ID"])
if "__REALITY_" in rendered:
    raise SystemExit("unreplaced Reality placeholder")
json.loads(rendered)
Path(os.environ["CEAVPN_OUTPUT"]).write_text(rendered, encoding="utf-8")
PY
unset CEAVPN_REALITY_PRIVATE_KEY
unset CEAVPN_REALITY_PUBLIC_KEY
chmod 0600 "$data_dir/xray_config.new.json"

"$data_dir/xray-core/xray" run -test \
  -c "$data_dir/xray_config.new.json"

if [[ -f "$data_dir/xray_config.json" ]]; then
  cp -a "$data_dir/xray_config.json" \
    "$data_dir/xray_config.json.before-reality.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$data_dir/xray_config.new.json" "$data_dir/xray_config.json"
docker compose -f "$compose_file" up -d --force-recreate

echo "Reality config applied"
echo "Public key and short ID are stored in $public_file"
