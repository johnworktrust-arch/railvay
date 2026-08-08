#!/usr/bin/env bash
# =============================================================================
# find_whitelist_candidate.sh
# Ищет кандидата на whitelist ingress из диапазонов VK Cloud (AS47764/AS28709).
#
# Метод:
#   1. Берём диапазоны VK из BGP (AS47764 + AS28709)
#   2. Генерируем список IP (первые N из каждого /22—/24)
#   3. Проверяем порты 80, 443, 8443
#   4. На каждом открытом 443 делаем TLS-рукопожатие — нужен TLS 1.3
#   5. Выводим список кандидатов в candidates.txt
#
# Требования: nmap, openssl, curl (установи через brew install nmap)
# Запуск: bash scripts/find_whitelist_candidate.sh
# =============================================================================
set -euo pipefail

OUTDIR="$(dirname "$0")/../data/whitelist_candidates"
mkdir -p "$OUTDIR"
CANDIDATES="$OUTDIR/candidates.txt"
LOG="$OUTDIR/scan.log"
> "$CANDIDATES"
> "$LOG"

echo "=== CeaVPN Whitelist Candidate Scanner ===" | tee -a "$LOG"
echo "Время: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# --- Диапазоны VK Cloud (AS47764 + AS28709) ---
# Выбираем самые перспективные /22-/24 блоки VK Cloud
# (инфраструктурные диапазоны, не CDN)
VK_RANGES=(
  # AS47764 - VK Cloud основные блоки
  "95.163.32.0/19"     # крупный VK Cloud блок
  "89.208.196.0/22"
  "89.208.208.0/22"
  "89.208.220.0/22"
  "87.239.104.0/21"
  "90.156.212.0/22"
  "90.156.216.0/22"
  "90.156.232.0/21"
  "185.241.192.0/22"
  "185.100.104.0/22"
  "185.5.136.0/22"
  "185.86.144.0/22"
  "45.84.128.0/22"
  "45.136.20.0/22"
  "5.188.140.0/22"
  "5.101.40.0/22"
  # AS28709 - дополнительные VK блоки
  "95.213.44.0/24"
  "95.213.45.0/24"
  "178.237.16.0/24"
  "178.237.17.0/24"
)

# Сколько IP проверять из каждого диапазона (первые N хостов)
HOSTS_PER_RANGE=20
# Timeout для проверки портов (сек)
PORT_TIMEOUT=2
# Timeout для TLS-рукопожатия
TLS_TIMEOUT=3

# ---- Функция: разложить CIDR → первые N IP ----
expand_cidr() {
  local cidr="$1"
  local count="$2"
  python3 - <<PYEOF
import ipaddress, sys
net = ipaddress.ip_network('$cidr', strict=False)
hosts = list(net.hosts())
for h in hosts[1:${count}+1]:   # пропускаем network address
    print(h)
PYEOF
}

# ---- Функция: проверить хост ----
check_host() {
  local ip="$1"
  local range="$2"

  # 1. Проверяем порт 443
  if ! timeout "$PORT_TIMEOUT" bash -c "echo >/dev/tcp/$ip/443" 2>/dev/null; then
    return
  fi

  # 2. TLS-рукопожатие — нужен TLS 1.3
  tls_out=$(timeout "$TLS_TIMEOUT" openssl s_client \
    -connect "${ip}:443" \
    -tls1_3 \
    -servername "vk.com" \
    -brief \
    2>/dev/null </dev/null || true)

  if ! echo "$tls_out" | grep -q "TLSv1.3"; then
    # Попробуем без SNI (plain TLS)
    tls_out=$(timeout "$TLS_TIMEOUT" openssl s_client \
      -connect "${ip}:443" \
      -tls1_3 \
      -brief \
      2>/dev/null </dev/null || true)
    if ! echo "$tls_out" | grep -q "TLSv1.3"; then
      return
    fi
  fi

  # 3. Получаем subject cert
  cert_subject=$(timeout "$TLS_TIMEOUT" openssl s_client \
    -connect "${ip}:443" \
    -brief \
    2>/dev/null </dev/null 2>&1 | \
    grep -o 'subject=.*' | head -1 || echo "unknown")

  # 4. Проверяем, не занят ли 80 (нам нужен для ACME)
  port80="closed"
  if timeout "$PORT_TIMEOUT" bash -c "echo >/dev/tcp/$ip/80" 2>/dev/null; then
    port80="open"
  fi

  # 5. Проверяем 8443 (нужен для subscription proxy)
  port8443="closed"
  if timeout "$PORT_TIMEOUT" bash -c "echo >/dev/tcp/$ip/8443" 2>/dev/null; then
    port8443="open"
  fi

  # 6. Reverse DNS
  rdns=$(dig +short -x "$ip" 2>/dev/null | head -1 | sed 's/\.$//' || echo "")
  [ -z "$rdns" ] && rdns="no-rdns"

  # Результат
  result="IP=$ip range=$range tls13=YES port80=$port80 port8443=$port8443 rdns=$rdns cert=$cert_subject"
  echo "  ✅ КАНДИДАТ: $result" | tee -a "$LOG"
  echo "$ip  # $range  rdns=$rdns  port80=$port80  port8443=$port8443" >> "$CANDIDATES"
}

# ---- Основной цикл ----
total_checked=0
for range in "${VK_RANGES[@]}"; do
  echo "🔍 Сканирую $range (первые $HOSTS_PER_RANGE хостов)..." | tee -a "$LOG"

  while IFS= read -r ip; do
    check_host "$ip" "$range" &
    # Ограничиваем параллельность
    if [[ $(jobs -r | wc -l) -ge 30 ]]; then
      wait -n 2>/dev/null || wait
    fi
    ((total_checked++)) || true
  done < <(expand_cidr "$range" "$HOSTS_PER_RANGE")

  wait  # дождаться завершения параллельных jobs
  echo "  Проверено IP в $range: $HOSTS_PER_RANGE" | tee -a "$LOG"
done

wait  # финальное ожидание

echo "" | tee -a "$LOG"
echo "=== Готово ===" | tee -a "$LOG"
echo "Всего проверено: $total_checked IP" | tee -a "$LOG"
echo "Кандидаты с TLS 1.3 на 443: $(wc -l < "$CANDIDATES")" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "📄 Файл кандидатов: $CANDIDATES"
echo "📋 Полный лог: $LOG"
echo ""

if [ -s "$CANDIDATES" ]; then
  echo "=== КАНДИДАТЫ ДЛЯ WHITELIST INGRESS ==="
  cat "$CANDIDATES"
  echo ""
  echo "➡️  Следующий шаг: выбери IP из списка выше,"
  echo "   зарегистрируй VPS у провайдера с этим IP,"
  echo "   и запусти provision согласно runbook §8.2"
else
  echo "❌ Кандидатов с TLS 1.3 на порту 443 не найдено в этих диапазонах."
  echo "   Попробуй расширить список диапазонов или увеличить HOSTS_PER_RANGE."
fi
