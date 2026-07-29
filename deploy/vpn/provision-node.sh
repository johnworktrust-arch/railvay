#!/usr/bin/env bash
set -Eeuo pipefail

bundle_dir="${1:-/tmp/ceavpn-bundle}"
node_file="${2:-/root/ceavpn-node.env}"
xray_rollback_dir=""
xray_upgrade_committed=0
managed_rollback_dir=""
managed_upgrade_committed=0
managed_paths=()
subscription_proxy_was_active=0
whitelist_timer_was_active=0
key_tmp=""

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

cleanup() {
  local status=$?
  local path backup_path
  trap - EXIT
  set +e
  if [[ -n "$key_tmp" ]]; then
    rm -f -- "$key_tmp"
  fi
  if (( status != 0 )) &&
    [[ -n "$managed_rollback_dir" || -n "$xray_rollback_dir" ]]; then
    systemctl stop ceavpn-whitelist-gate.timer \
      ceavpn-whitelist-gate.service \
      ceavpn-subscription-proxy.service >/dev/null 2>&1 || true
    if [[ -n "$managed_rollback_dir" ]] &&
      (( ! managed_upgrade_committed )); then
      for path in "${managed_paths[@]}"; do
        backup_path="${managed_rollback_dir}/rootfs${path}"
        install -d -o root -g root -m 0755 "$(dirname "$path")"
        rm -f -- "$path"
        if [[ -e "$backup_path" || -L "$backup_path" ]]; then
          cp -a "$backup_path" "$path"
        fi
      done
    fi
    if [[ -n "$xray_rollback_dir" ]] &&
      (( ! xray_upgrade_committed )); then
      find /var/lib/marzban/xray-core -mindepth 1 -delete
      cp -a "$xray_rollback_dir/." /var/lib/marzban/xray-core/
      chown -R root:root /var/lib/marzban/xray-core
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if (( subscription_proxy_was_active )); then
      systemctl start ceavpn-subscription-proxy.service \
        >/dev/null 2>&1 || true
    fi
    if (( whitelist_timer_was_active )); then
      systemctl start ceavpn-whitelist-gate.timer \
        >/dev/null 2>&1 || true
    fi
    if [[ -s /opt/marzban/docker-compose.yml ]]; then
      docker compose -f /opt/marzban/docker-compose.yml \
        up -d --force-recreate >/dev/null 2>&1 || true
    fi
    nginx -t -q >/dev/null 2>&1 && systemctl reload nginx \
      >/dev/null 2>&1 || true
  fi
  if [[ -n "$xray_rollback_dir" ]]; then
    find "$xray_rollback_dir" -mindepth 1 -delete
    rmdir "$xray_rollback_dir" 2>/dev/null || true
  fi
  if [[ -n "$managed_rollback_dir" ]]; then
    find "$managed_rollback_dir" -mindepth 1 -delete
    rmdir "$managed_rollback_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

backup_managed_files() {
  local rollback_candidate path backup_path
  rollback_candidate="$(mktemp -d /root/ceavpn-managed-rollback.XXXXXX)"
  chmod 0700 "$rollback_candidate"
  if systemctl is-active --quiet ceavpn-subscription-proxy.service; then
    subscription_proxy_was_active=1
  fi
  if systemctl is-active --quiet ceavpn-whitelist-gate.timer; then
    whitelist_timer_was_active=1
  fi
  for path in "${managed_paths[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
      backup_path="${rollback_candidate}/rootfs${path}"
      install -d -o root -g root -m 0700 "$(dirname "$backup_path")"
      if ! cp -a "$path" "$backup_path"; then
        find "$rollback_candidate" -mindepth 1 -delete
        rmdir "$rollback_candidate" 2>/dev/null || true
        echo "could not back up managed VPN files" >&2
        return 1
      fi
    fi
  done
  managed_rollback_dir="$rollback_candidate"
}

for path in \
  "$bundle_dir/docker-compose.yml" \
  "$bundle_dir/nginx.conf" \
  "$bundle_dir/connect.html" \
  "$bundle_dir/apply-reality-config.sh" \
  "$bundle_dir/configure-marzban-hosts.sh" \
  "$bundle_dir/install-worker.sh" \
  "$bundle_dir/worker.py" \
  "$bundle_dir/ceavpn-worker.service" \
  "$bundle_dir/subscription_proxy.py" \
  "$bundle_dir/ceavpn-subscription-proxy.service" \
  "$bundle_dir/marzban" \
  "$bundle_dir/xray-core/xray" \
  "$node_file"; do
  if [[ ! -s "$path" ]]; then
    echo "missing required file: $path" >&2
    exit 1
  fi
done

umask 077
# shellcheck disable=SC1090
source "$node_file"

: "${CEAVPN_PUBLIC_IP:?CEAVPN_PUBLIC_IP is required}"
: "${CEAVPN_SUB_DOMAIN:?CEAVPN_SUB_DOMAIN is required}"
: "${CEAVPN_COVER_DOMAIN:?CEAVPN_COVER_DOMAIN is required}"
: "${CEAVPN_REGION_REMARK:?CEAVPN_REGION_REMARK is required}"

node_mode="${CEAVPN_NODE_MODE:-direct}"
case "$node_mode" in
  direct)
    xray_template_source="$bundle_dir/xray_config.json"
    ;;
  lte)
    xray_template_source="$bundle_dir/xray_config_lte.json"
    if [[ ! -s /root/ceavpn-lte-exit.env ]]; then
      echo "missing required file: /root/ceavpn-lte-exit.env" >&2
      exit 1
    fi
    chmod 0600 /root/ceavpn-lte-exit.env
    ;;
  whitelist)
    xray_template_source="$bundle_dir/xray_config_whitelist.json"
    : "${CEAVPN_SERVER_CODE:?CEAVPN_SERVER_CODE is required}"
    if [[ ! "$CEAVPN_SERVER_CODE" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
      echo "invalid CEAVPN_SERVER_CODE" >&2
      exit 1
    fi
    for path in \
      "$bundle_dir/xray-pins.env" \
      "$bundle_dir/qualify-whitelist-ingress.sh" \
      "$bundle_dir/configure-whitelist-host.sh" \
      "$bundle_dir/ceavpn-whitelist-boot-close.service" \
      "$bundle_dir/ceavpn-whitelist-gate.service" \
      "$bundle_dir/ceavpn-whitelist-gate.timer" \
      /root/ceavpn-lte-exit.env; do
      if [[ ! -s "$path" ]]; then
        echo "missing required file: $path" >&2
        exit 1
      fi
    done
    chmod 0600 /root/ceavpn-lte-exit.env
    ;;
  *)
    echo "CEAVPN_NODE_MODE must be direct, lte, or whitelist" >&2
    exit 1
    ;;
esac
if [[ ! -s "$xray_template_source" ]]; then
  echo "missing required file: $xray_template_source" >&2
  exit 1
fi

if [[ "$node_mode" == "whitelist" ]]; then
  # The official XHTTP example requires Xray >= 25.3.6. Whitelist nodes use
  # one reviewed stable release and verify the extracted executable before it
  # can replace an installed binary. Direct/LTE nodes retain their old install
  # behavior and are not upgraded by this path.
  # shellcheck disable=SC1090
  source "$bundle_dir/xray-pins.env"
  : "${CEAVPN_XRAY_REQUIRED_VERSION:?missing pinned Xray version}"
  case "$(dpkg --print-architecture)" in
    amd64)
      expected_xray_sha256="${CEAVPN_XRAY_SHA256_AMD64:-}"
      ;;
    arm64)
      expected_xray_sha256="${CEAVPN_XRAY_SHA256_ARM64:-}"
      ;;
    *)
      echo "whitelist mode supports pinned Xray only on amd64 or arm64" >&2
      exit 1
      ;;
  esac
  if [[ ! "$CEAVPN_XRAY_REQUIRED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    [[ ! "$expected_xray_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "invalid Xray pin file" >&2
    exit 1
  fi
  actual_xray_sha256="$(sha256sum "$bundle_dir/xray-core/xray" | awk '{print $1}')"
  if [[ "$actual_xray_sha256" != "$expected_xray_sha256" ]]; then
    echo "bundled Xray executable does not match the pinned official digest" >&2
    exit 1
  fi
  bundled_xray_version="$("$bundle_dir/xray-core/xray" version)"
  read -r bundled_xray_name bundled_xray_release _ <<<"$bundled_xray_version"
  if [[ "$bundled_xray_name" != "Xray" ]] ||
    [[ "$bundled_xray_release" != "$CEAVPN_XRAY_REQUIRED_VERSION" ]]; then
    echo "bundled Xray executable does not match the pinned version" >&2
    exit 1
  fi
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
  ca-certificates certbot conntrack curl docker-compose-v2 docker.io fail2ban jq \
  nginx openssl python3 unattended-upgrades ufw

if [[ "$node_mode" == "whitelist" ]]; then
  if [[ "$CEAVPN_COVER_DOMAIN" == "$CEAVPN_SUB_DOMAIN" ]]; then
    echo "whitelist REALITY camouflage must be a remote domain" >&2
    exit 1
  fi
  if ! CEAVPN_REMOTE_COVER_DOMAIN="$CEAVPN_COVER_DOMAIN" \
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
    raise SystemExit("camouflage target is not a stable remote public endpoint")
PY
  then
    echo "remote REALITY camouflage DNS validation failed" >&2
    exit 1
  fi
  cover_tls_probe="$(
    timeout 20 openssl s_client \
      -connect "${CEAVPN_COVER_DOMAIN}:443" \
      -servername "$CEAVPN_COVER_DOMAIN" \
      -verify_return_error \
      -verify_hostname "$CEAVPN_COVER_DOMAIN" \
      -tls1_3 -alpn h2 </dev/null 2>&1
  )" || {
    echo "remote REALITY camouflage TLS validation failed" >&2
    exit 1
  }
  if ! grep -q 'ALPN protocol: h2' <<<"$cover_tls_probe" ||
    ! grep -q 'Verify return code: 0 (ok)' <<<"$cover_tls_probe"; then
    echo "remote REALITY camouflage must provide verified TLS 1.3 and HTTP/2" >&2
    exit 1
  fi
  cover_headers="$(mktemp /run/ceavpn-cover-headers.XXXXXX)"
  chmod 0600 "$cover_headers"
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
    rm -f -- "$cover_headers"
    echo "remote REALITY camouflage HTTP endpoint is redirecting or unstable" >&2
    exit 1
  fi
  rm -f -- "$cover_headers"
  unset cover_headers cover_http_code
  unset cover_tls_probe
fi

systemctl enable --now docker nginx fail2ban
timedatectl set-timezone UTC
timedatectl set-ntp true

managed_paths=(
  /opt/marzban/docker-compose.yml
  /opt/marzban/xray_config.template.json
  /opt/marzban/nginx.template.conf
  /opt/marzban/connect.html
  /opt/marzban/.env
  /opt/ceavpn/apply-reality-config.sh
  /opt/ceavpn/configure-marzban-hosts.sh
  /opt/ceavpn/install-worker.sh
  /opt/ceavpn/worker.py
  /opt/ceavpn/ceavpn-worker.service
  /opt/ceavpn/subscription_proxy.py
  /opt/ceavpn/qualify-whitelist-ingress.sh
  /opt/ceavpn/configure-whitelist-host.sh
  /usr/local/bin/marzban
  /etc/systemd/system/ceavpn-subscription-proxy.service
  /etc/systemd/system/ceavpn-whitelist-boot-close.service
  /etc/systemd/system/ceavpn-whitelist-gate.service
  /etc/systemd/system/ceavpn-whitelist-gate.timer
  /etc/systemd/system/multi-user.target.wants/ceavpn-subscription-proxy.service
  /etc/systemd/system/multi-user.target.wants/ceavpn-whitelist-boot-close.service
  /etc/systemd/system/timers.target.wants/ceavpn-whitelist-gate.timer
  /etc/nginx/snippets/ceavpn-relays.conf
  /etc/nginx/sites-available/ceavpn-acme
  /etc/nginx/sites-enabled/ceavpn
  /etc/nginx/sites-enabled/default
  /var/lib/marzban/xray_config.json
)
backup_managed_files

install -d -o root -g root -m 0755 \
  /opt/marzban /opt/ceavpn /etc/ceavpn /var/lib/marzban \
  /var/www/letsencrypt /etc/nginx/snippets
if [[ "$node_mode" == "whitelist" ]]; then
  install -d -o root -g root -m 0755 /var/www/ceavpn-whitelist
fi
install -d -o root -g root -m 0755 /var/lib/marzban/xray-core
if [[ ! -e /etc/nginx/snippets/ceavpn-relays.conf ]]; then
  install -o root -g root -m 0644 /dev/null \
    /etc/nginx/snippets/ceavpn-relays.conf
fi

install -o root -g root -m 0644 \
  "$bundle_dir/docker-compose.yml" /opt/marzban/docker-compose.yml
install -o root -g root -m 0644 \
  "$xray_template_source" /opt/marzban/xray_config.template.json
install -o root -g root -m 0644 \
  "$bundle_dir/nginx.conf" /opt/marzban/nginx.template.conf
install -o root -g root -m 0644 \
  "$bundle_dir/connect.html" /opt/marzban/connect.html
install -o root -g root -m 0755 \
  "$bundle_dir/apply-reality-config.sh" /opt/ceavpn/apply-reality-config.sh
install -o root -g root -m 0755 \
  "$bundle_dir/configure-marzban-hosts.sh" \
  /opt/ceavpn/configure-marzban-hosts.sh
install -o root -g root -m 0755 \
  "$bundle_dir/install-worker.sh" /opt/ceavpn/install-worker.sh
install -o root -g root -m 0755 \
  "$bundle_dir/worker.py" /opt/ceavpn/worker.py
install -o root -g root -m 0644 \
  "$bundle_dir/ceavpn-worker.service" \
  /opt/ceavpn/ceavpn-worker.service
install -o root -g root -m 0755 \
  "$bundle_dir/subscription_proxy.py" \
  /opt/ceavpn/subscription_proxy.py
install -o root -g root -m 0644 \
  "$bundle_dir/ceavpn-subscription-proxy.service" \
  /etc/systemd/system/ceavpn-subscription-proxy.service
if [[ "$node_mode" == "whitelist" ]]; then
  install -o root -g root -m 0755 \
    "$bundle_dir/qualify-whitelist-ingress.sh" \
    /opt/ceavpn/qualify-whitelist-ingress.sh
  install -o root -g root -m 0755 \
    "$bundle_dir/configure-whitelist-host.sh" \
    /opt/ceavpn/configure-whitelist-host.sh
  install -o root -g root -m 0644 \
    "$bundle_dir/ceavpn-whitelist-boot-close.service" \
    /etc/systemd/system/ceavpn-whitelist-boot-close.service
  install -o root -g root -m 0644 \
    "$bundle_dir/ceavpn-whitelist-gate.service" \
    /etc/systemd/system/ceavpn-whitelist-gate.service
  install -o root -g root -m 0644 \
    "$bundle_dir/ceavpn-whitelist-gate.timer" \
    /etc/systemd/system/ceavpn-whitelist-gate.timer
fi
systemctl daemon-reload
systemctl enable --now ceavpn-subscription-proxy.service
install -o root -g root -m 0755 "$bundle_dir/marzban" /usr/local/bin/marzban
if [[ "$node_mode" == "whitelist" ]]; then
  rollback_candidate="$(mktemp -d /root/ceavpn-xray-rollback.XXXXXX)"
  chmod 0700 "$rollback_candidate"
  if ! cp -a /var/lib/marzban/xray-core/. "$rollback_candidate/"; then
    find "$rollback_candidate" -mindepth 1 -delete
    rmdir "$rollback_candidate" 2>/dev/null || true
    echo "could not back up the installed Xray runtime" >&2
    exit 1
  fi
  xray_rollback_dir="$rollback_candidate"
  unset rollback_candidate
  xray_candidate="/var/lib/marzban/xray-core/xray.new"
  install -o root -g root -m 0755 \
    "$bundle_dir/xray-core/xray" "$xray_candidate"
  candidate_sha256="$(sha256sum "$xray_candidate" | awk '{print $1}')"
  if [[ "$candidate_sha256" != "$expected_xray_sha256" ]]; then
    echo "staged Xray executable failed digest verification" >&2
    exit 1
  fi
  find "$bundle_dir/xray-core" -mindepth 1 -maxdepth 1 \
    ! -name xray -exec cp -a -- {} /var/lib/marzban/xray-core/ \;
  mv "$xray_candidate" /var/lib/marzban/xray-core/xray
elif [[ ! -x /var/lib/marzban/xray-core/xray ]]; then
  cp -a "$bundle_dir/xray-core/." /var/lib/marzban/xray-core/
fi
chown -R root:root /var/lib/marzban/xray-core
chmod 0755 /var/lib/marzban/xray-core/xray

cat > /opt/marzban/.env <<EOF
UVICORN_HOST="127.0.0.1"
UVICORN_PORT=8000
XRAY_JSON="/var/lib/marzban/xray_config.json"
XRAY_EXECUTABLE_PATH="/var/lib/marzban/xray-core/xray"
XRAY_ASSETS_PATH="/var/lib/marzban/xray-core"
SQLALCHEMY_DATABASE_URL="sqlite:////var/lib/marzban/db.sqlite3"
XRAY_SUBSCRIPTION_URL_PREFIX="https://${CEAVPN_SUB_DOMAIN}:8443"
XRAY_SUBSCRIPTION_PATH="sub"
DOCS=False
DEBUG=False
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=60
USERS_AUTODELETE_DAYS=-1
NOTIFY_LOGIN=False
SUB_PROFILE_TITLE="CEA VPN"
SUB_UPDATE_INTERVAL="1"
EOF
chmod 0600 /opt/marzban/.env

acme_server_names="${CEAVPN_SUB_DOMAIN} ${CEAVPN_COVER_DOMAIN}"
certbot_domains=(-d "$CEAVPN_SUB_DOMAIN" -d "$CEAVPN_COVER_DOMAIN")
if [[ "$node_mode" == "whitelist" ]]; then
  acme_server_names="$CEAVPN_SUB_DOMAIN"
  certbot_domains=(-d "$CEAVPN_SUB_DOMAIN")
fi

cat > /etc/nginx/sites-available/ceavpn-acme <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${acme_server_names};
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        try_files \$uri =404;
    }
    location / { return 404; }
}
EOF
ln -sfn /etc/nginx/sites-available/ceavpn-acme \
  /etc/nginx/sites-enabled/ceavpn
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [[ ! -s "/etc/letsencrypt/live/${CEAVPN_SUB_DOMAIN}/fullchain.pem" ]]; then
  certbot certonly --non-interactive --agree-tos \
    --register-unsafely-without-email \
    --webroot -w /var/www/letsencrypt \
    "${certbot_domains[@]}"
fi

if [[ ! -s /root/ceavpn-reality-keys.txt ]]; then
  key_tmp="$(mktemp /root/ceavpn-reality-keys.txt.new.XXXXXX)"
  /var/lib/marzban/xray-core/xray x25519 >"$key_tmp"
  grep -Eq "^(Private key|PrivateKey):[[:space:]]*[^[:space:]]" "$key_tmp"
  grep -Eq "^(Public key|Password \\(PublicKey\\)):[[:space:]]*[^[:space:]]" \
    "$key_tmp"
  chmod 0600 "$key_tmp"
  mv "$key_tmp" /root/ceavpn-reality-keys.txt
  key_tmp=""
fi

/opt/ceavpn/apply-reality-config.sh

for _ in {1..60}; do
  if curl -fsS --connect-timeout 2 http://127.0.0.1:8000/dashboard/ \
    -o /dev/null 2>/dev/null; then
    break
  fi
  sleep 1
done

if [[ ! -s /root/ceavpn-admin.env ]]; then
  worker_password="$(openssl rand -base64 48 | tr -d '\n')"
  docker compose -f /opt/marzban/docker-compose.yml exec -T \
    -e CLI_PROG_NAME="marzban cli" \
    -e MARZBAN_ADMIN_PASSWORD="$worker_password" \
    marzban marzban-cli admin create \
    -u cea-railway-bot --no-sudo -tg 0 -dc 0
  printf 'MARZBAN_BOT_USERNAME=%q\nMARZBAN_BOT_PASSWORD=%q\n' \
    'cea-railway-bot' "$worker_password" > /root/ceavpn-admin.env
  chmod 0600 /root/ceavpn-admin.env
  unset worker_password
fi

if [[ ! -s /root/ceavpn-sudo-admin.env ]]; then
  sudo_password="$(openssl rand -base64 48 | tr -d '\n')"
  docker compose -f /opt/marzban/docker-compose.yml exec -T \
    -e CLI_PROG_NAME="marzban cli" \
    -e MARZBAN_ADMIN_PASSWORD="$sudo_password" \
    marzban marzban-cli admin create \
    -u cea-hosts-admin --sudo -tg 0 -dc 0
  printf 'MARZBAN_SUDO_USERNAME=%q\nMARZBAN_SUDO_PASSWORD=%q\n' \
    'cea-hosts-admin' "$sudo_password" > /root/ceavpn-sudo-admin.env
  chmod 0600 /root/ceavpn-sudo-admin.env
  unset sudo_password
fi

/opt/ceavpn/configure-marzban-hosts.sh

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
if [[ "$node_mode" == "whitelist" ]]; then
  ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
  ufw deny 443/tcp
else
  ufw allow 443/tcp
fi
ufw allow 8443/tcp
ufw deny out 25/tcp
ufw --force enable

if [[ "$node_mode" == "whitelist" ]]; then
  /opt/ceavpn/qualify-whitelist-ingress.sh enforce
  systemctl enable ceavpn-whitelist-boot-close.service
  systemctl enable --now ceavpn-whitelist-gate.timer
fi

systemctl is-active docker nginx fail2ban
docker compose -f /opt/marzban/docker-compose.yml ps
ss -lntp

xray_upgrade_committed=1
managed_upgrade_committed=1
if [[ -n "$xray_rollback_dir" ]]; then
  find "$xray_rollback_dir" -mindepth 1 -delete
  rmdir "$xray_rollback_dir"
  xray_rollback_dir=""
fi
find "$managed_rollback_dir" -mindepth 1 -delete
rmdir "$managed_rollback_dir"
managed_rollback_dir=""
