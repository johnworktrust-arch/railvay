#!/usr/bin/env bash
set -Eeuo pipefail

bundle_dir="${1:-/tmp/ceavpn-bundle}"
node_file="${2:-/root/ceavpn-node.env}"

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

for path in \
  "$bundle_dir/docker-compose.yml" \
  "$bundle_dir/xray_config.json" \
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

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
  ca-certificates certbot curl docker-compose-v2 docker.io fail2ban jq \
  nginx openssl python3 unattended-upgrades ufw

systemctl enable --now docker nginx fail2ban
timedatectl set-timezone UTC
timedatectl set-ntp true

install -d -o root -g root -m 0755 \
  /opt/marzban /opt/ceavpn /etc/ceavpn /var/lib/marzban \
  /var/www/letsencrypt /etc/nginx/snippets
install -d -o root -g root -m 0755 /var/lib/marzban/xray-core
if [[ ! -e /etc/nginx/snippets/ceavpn-relays.conf ]]; then
  install -o root -g root -m 0644 /dev/null \
    /etc/nginx/snippets/ceavpn-relays.conf
fi

install -o root -g root -m 0644 \
  "$bundle_dir/docker-compose.yml" /opt/marzban/docker-compose.yml
install -o root -g root -m 0644 \
  "$bundle_dir/xray_config.json" /opt/marzban/xray_config.template.json
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
systemctl daemon-reload
systemctl enable --now ceavpn-subscription-proxy.service
install -o root -g root -m 0755 "$bundle_dir/marzban" /usr/local/bin/marzban
if [[ ! -x /var/lib/marzban/xray-core/xray ]]; then
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

cat > /etc/nginx/sites-available/ceavpn-acme <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${CEAVPN_SUB_DOMAIN} ${CEAVPN_COVER_DOMAIN};
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
    -d "$CEAVPN_SUB_DOMAIN" -d "$CEAVPN_COVER_DOMAIN"
fi

if [[ ! -s /root/ceavpn-reality-keys.txt ]]; then
  key_tmp="$(mktemp /root/ceavpn-reality-keys.txt.new.XXXXXX)"
  trap 'rm -f -- "${key_tmp:-}"' EXIT
  /var/lib/marzban/xray-core/xray x25519 >"$key_tmp"
  grep -Eq "^(Private key|PrivateKey):[[:space:]]*[^[:space:]]" "$key_tmp"
  grep -Eq "^(Public key|Password \\(PublicKey\\)):[[:space:]]*[^[:space:]]" \
    "$key_tmp"
  chmod 0600 "$key_tmp"
  mv "$key_tmp" /root/ceavpn-reality-keys.txt
  trap - EXIT
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
ufw allow 443/tcp
ufw allow 8443/tcp
ufw deny out 25/tcp
ufw --force enable

systemctl is-active docker nginx fail2ban
docker compose -f /opt/marzban/docker-compose.yml ps
ss -lntp
