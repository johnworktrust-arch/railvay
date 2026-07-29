"use strict";

const token = document.querySelector('meta[name="cea-admin-token"]').content;
const state = {
  product: "ai",
  view: "overview",
  segment: "all",
  query: "",
  page: 1,
  pages: 1,
  vpnSegment: "all",
  vpnQuery: "",
  vpnPage: 1,
  vpnPages: 1,
  selectedUserId: null,
  selectedUserKind: null,
  canManage: false,
  maintenance: false,
  adminUsers: 0,
};

const formatNumber = new Intl.NumberFormat("ru-RU");
const formatMoney = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});
const formatDate = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const formatDateOnly = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

function byId(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function asDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : formatDate.format(date);
}

function asDateOnly(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : formatDateOnly.format(date);
}

function coinLabel(value) {
  return `${formatNumber.format(Number(value || 0))} коин`;
}

async function api(path, options = {}) {
  const headers = {
    "X-Cea-Admin-Token": token,
    ...(options.headers || {}),
  };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Сессия истекла");
  }
  if (!response.ok) {
    throw new Error(payload.error || `Ошибка ${response.status}`);
  }
  return payload;
}

let toastTimer;
function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

function setProduct(product) {
  state.product = product === "vpn" ? "vpn" : "ai";
  closeDrawer();
  document.querySelectorAll(".product-card").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.product === state.product);
  });
  document.querySelectorAll("[data-product-nav]").forEach((group) => {
    group.hidden = group.dataset.productNav !== state.product;
  });
  byId("product-eyebrow").textContent = state.product === "vpn" ? "Cea VPN" : "Cea AI";
  byId("maintenance-control").hidden = state.product === "vpn";
  setView(state.product === "vpn" ? "vpn-overview" : "overview");
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("is-active", section.id === `${view}-view`);
  });
  const titles = {
    overview: "Обзор",
    users: "Пользователи",
    "vpn-overview": "Обзор VPN",
    "vpn-users": "VPN-пользователи",
  };
  byId("page-title").textContent = titles[view] || "Обзор";
  if (view === "overview") {
    loadStats().catch((error) => showToast(error.message, true));
  }
  if (view === "users") loadUsers();
  if (view === "vpn-overview") {
    loadVpnStats().catch((error) => showToast(error.message, true));
  }
  if (view === "vpn-users") loadVpnUsers();
}

function setMaintenance(active) {
  state.maintenance = Boolean(active);
  const toggle = byId("maintenance-toggle");
  toggle.setAttribute("aria-checked", String(state.maintenance));
  byId("maintenance-label").textContent = state.maintenance
    ? "Техработы включены"
    : "Техработы выключены";
  byId("service-maintenance").textContent = state.maintenance
    ? "Включены"
    : "Выключены";
  byId("service-maintenance").style.color = state.maintenance
    ? "var(--red)"
    : "var(--green)";
}

async function loadStatus() {
  const data = await api("/api/status");
  state.canManage = Boolean(data.can_manage);
  byId("database-label").textContent = data.database;
  byId("access-label").textContent = state.canManage
    ? "Управление доступно"
    : "Только просмотр";
  byId("maintenance-toggle").disabled = !state.canManage;
  setMaintenance(data.maintenance_active);
}

function metric(id, value) {
  byId(id).textContent = formatNumber.format(Number(value || 0));
}

async function loadStats() {
  const data = await api("/api/stats");
  state.adminUsers = Number(data.admin_users || 0);
  metric("metric-users", data.users_total);
  byId("metric-users-today").textContent =
    `+${formatNumber.format(data.users_period || 0)} за сегодня`;
  metric("metric-paid", data.paid_users);
  byId("metric-conversion").textContent =
    `${data.conversion_percent || 0}% конверсия`;
  metric("metric-trial", data.trial_users);
  byId("metric-active-trial").textContent =
    `${formatNumber.format(data.active_trial_users || 0)} активны`;
  metric("metric-active", data.active_subscriptions);
  byId("metric-active-paid").textContent =
    `${formatNumber.format(data.active_paid_users || 0)} платных`;
  byId("metric-revenue").textContent = formatMoney.format(data.revenue_rub || 0);
  byId("metric-revenue-today").textContent =
    `${formatMoney.format(data.revenue_period_rub || 0)} за сегодня`;
  metric("metric-generations", data.generations_total);
  byId("metric-generations-today").textContent =
    `+${formatNumber.format(data.generations_period || 0)} за сегодня`;

  const total = Number(data.users_total || 0);
  const paid = Number(data.paid_users || 0);
  const trial = Number(data.trial_users || 0);
  const other = Math.max(total - paid - trial, 0);
  byId("breakdown-paid").textContent = formatNumber.format(paid);
  byId("breakdown-trial").textContent = formatNumber.format(trial);
  byId("breakdown-other").textContent = formatNumber.format(other);
  byId("bar-paid").style.width = `${total ? (paid / total) * 100 : 0}%`;
  byId("bar-trial").style.width = `${total ? (trial / total) * 100 : 0}%`;
  byId("bar-other").style.width = `${total ? (other / total) * 100 : 0}%`;
  byId("service-blocked").textContent = formatNumber.format(data.blocked_users || 0);
  byId("service-admins").textContent = formatNumber.format(data.admin_users || 0);
  byId("service-balance").textContent = coinLabel(data.active_balance_total);
  byId("service-payments").textContent = formatNumber.format(data.paid_payments || 0);
  byId("service-platega").textContent =
    `${formatNumber.format(data.platega_paid_payments || 0)} · ${formatMoney.format(data.platega_revenue_rub || 0)}`;
  byId("service-stars").textContent =
    formatNumber.format(data.stars_paid_payments || 0);
}

function serverListMarkup(servers) {
  if (!servers?.length) {
    return '<div class="empty-list">VPN-серверы не настроены</div>';
  }
  return servers.map((server) => {
    const online = Boolean(server.is_healthy);
    const jobs = Number(server.queued_jobs || 0);
    const failures = Number(server.failed_jobs || 0);
    const details = [
      `${formatNumber.format(server.active_subscriptions || 0)} активных подписок`,
      jobs ? `${formatNumber.format(jobs)} в очереди` : "очередь пуста",
      failures ? `${formatNumber.format(failures)} ошибок` : "без ошибок",
    ].join(" · ");
    return `
      <div class="server-row">
        <div>
          <strong>${escapeHtml(server.name || server.code)}</strong>
          <span>${escapeHtml(details)}</span>
        </div>
        <div class="server-state ${online ? "is-online" : ""}">
          ${online ? "Онлайн" : "Нет связи"}
        </div>
      </div>
    `;
  }).join("");
}

async function loadVpnStats() {
  const data = await api("/api/vpn/stats");
  metric("vpn-metric-users", data.users_total);
  byId("vpn-metric-conversion").textContent =
    `${data.conversion_percent || 0}% оплатили`;
  metric("vpn-metric-active", data.active_users);
  byId("vpn-metric-active-paid").textContent =
    `${formatNumber.format(data.active_paid_users || 0)} платных`;
  metric("vpn-metric-trial", data.trial_users);
  byId("vpn-metric-active-trial").textContent =
    `${formatNumber.format(data.active_trial_users || 0)} активны`;
  metric("vpn-metric-paid", data.paid_users);
  byId("vpn-metric-payments").textContent =
    `${formatNumber.format(data.paid_payments || 0)} платежей`;
  byId("vpn-metric-revenue").textContent =
    formatMoney.format(data.revenue_rub || 0);
  byId("vpn-metric-revenue-today").textContent =
    `${formatMoney.format(data.revenue_period_rub || 0)} за сегодня`;
  byId("vpn-metric-servers").textContent =
    `${formatNumber.format(data.servers_healthy || 0)} / ${formatNumber.format(data.servers_total || 0)}`;
  const issueCount = Number(data.error_subscriptions || 0) + Number(data.failed_jobs || 0);
  byId("vpn-metric-server-errors").textContent =
    `${formatNumber.format(issueCount)} ошибок`;

  const active = Number(data.active_users || 0);
  const expired = Number(data.expired_subscriptions || 0);
  const provisioning = Number(data.provisioning_subscriptions || 0);
  const errors = Number(data.error_subscriptions || 0);
  const total = Math.max(active + expired + provisioning + errors, 1);
  byId("vpn-breakdown-active").textContent = formatNumber.format(active);
  byId("vpn-breakdown-expired").textContent = formatNumber.format(expired);
  byId("vpn-breakdown-provisioning").textContent = formatNumber.format(provisioning);
  byId("vpn-breakdown-errors").textContent = formatNumber.format(errors);
  byId("vpn-bar-active").style.width = `${(active / total) * 100}%`;
  byId("vpn-bar-expired").style.width = `${(expired / total) * 100}%`;
  byId("vpn-bar-provisioning").style.width = `${(provisioning / total) * 100}%`;
  byId("vpn-bar-errors").style.width = `${(errors / total) * 100}%`;
  byId("vpn-server-list").innerHTML = serverListMarkup(data.servers);
}

function segmentBadge(user) {
  if (user.is_admin) return '<span class="badge admin">Администратор</span>';
  if (user.has_paid) return '<span class="badge paid">Платил</span>';
  if (user.has_trial) return '<span class="badge trial">Бесплатный</span>';
  return '<span class="badge none">Без доступа</span>';
}

function statusBadge(user) {
  if (user.is_blocked) return '<span class="badge blocked">Заблокирован</span>';
  if (user.subscription_is_active) return '<span class="badge active">Активен</span>';
  return '<span class="badge inactive">Без подписки</span>';
}

function userName(user) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return name || (user.username ? `@${user.username}` : `ID ${user.telegram_id}`);
}

function usersMarkup(users) {
  if (!users.length) {
    return '<tr><td colspan="7" class="empty-cell">По этому фильтру пользователей нет</td></tr>';
  }
  return users.map((user) => `
    <tr data-user-id="${Number(user.id)}" tabindex="0">
      <td>
        <div class="user-cell">
          <strong>${escapeHtml(userName(user))}</strong>
          <span>${user.username ? `@${escapeHtml(user.username)} · ` : ""}TG ${escapeHtml(user.telegram_id)}</span>
          <span class="mobile-registration">Рег.: ${escapeHtml(asDateOnly(user.created_at))}</span>
        </div>
      </td>
      <td>${segmentBadge(user)}</td>
      <td>${escapeHtml(user.plan_name || "—")}</td>
      <td>${escapeHtml(coinLabel(user.coins_balance_cache))}</td>
      <td>${escapeHtml(asDate(user.created_at))}</td>
      <td>${escapeHtml(asDate(user.last_seen_at))}</td>
      <td>${statusBadge(user)}</td>
    </tr>
  `).join("");
}

async function loadUsers() {
  byId("users-body").innerHTML =
    '<tr><td colspan="7" class="loading-cell">Загрузка…</td></tr>';
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: "25",
    segment: state.segment,
  });
  if (state.query) params.set("q", state.query);
  try {
    const data = await api(`/api/users?${params}`);
    state.page = data.page;
    state.pages = data.pages;
    byId("users-body").innerHTML = usersMarkup(data.users);
    const serviceSuffix = state.segment === "all" && state.adminUsers
      ? ` · служебных: ${formatNumber.format(state.adminUsers)}`
      : "";
    byId("users-count").textContent =
      `${formatNumber.format(data.total)} записей${serviceSuffix}`;
    byId("users-page-label").textContent = `Страница ${data.page} из ${data.pages}`;
    byId("pagination-label").textContent = `${data.page} / ${data.pages}`;
    byId("previous-page").disabled = data.page <= 1;
    byId("next-page").disabled = data.page >= data.pages;
  } catch (error) {
    byId("users-body").innerHTML =
      `<tr><td colspan="7" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
    showToast(error.message, true);
  }
}

function vpnTypeBadge(user) {
  if (user.vpn_has_paid || user.vpn_billing_kind === "paid") {
    return '<span class="badge paid">Платный</span>';
  }
  if (user.vpn_has_trial) {
    return '<span class="badge trial">Пробный</span>';
  }
  return '<span class="badge none">Без тарифа</span>';
}

function vpnStatusBadge(user) {
  const status = user.vpn_is_active ? "active" : (user.vpn_status || "none");
  const labels = {
    active: "Активна",
    provisioning: "Подключается",
    expired: "Истекла",
    disabled: "Отключена",
    error: "Ошибка",
    none: "Нет подписки",
  };
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(labels[status] || status)}</span>`;
}

function vpnUsersMarkup(users) {
  if (!users.length) {
    return '<tr><td colspan="7" class="empty-cell">По этому фильтру VPN-пользователей нет</td></tr>';
  }
  return users.map((user) => `
    <tr data-vpn-user-id="${Number(user.id)}" tabindex="0">
      <td>
        <div class="user-cell">
          <strong>${escapeHtml(userName(user))}</strong>
          <span>${user.username ? `@${escapeHtml(user.username)} · ` : ""}TG ${escapeHtml(user.telegram_id)}</span>
          <span class="mobile-registration">До: ${escapeHtml(asDateOnly(user.vpn_ends_at))}</span>
        </div>
      </td>
      <td>${vpnTypeBadge(user)}</td>
      <td>${escapeHtml(user.vpn_plan_name || (user.vpn_has_trial ? "3 дня бесплатно" : "—"))}</td>
      <td>${escapeHtml(formatMoney.format(user.vpn_paid_amount_rub || 0))}</td>
      <td>${escapeHtml(asDate(user.vpn_starts_at))}</td>
      <td>${escapeHtml(asDate(user.vpn_ends_at))}</td>
      <td>${vpnStatusBadge(user)}</td>
    </tr>
  `).join("");
}

async function loadVpnUsers() {
  byId("vpn-users-body").innerHTML =
    '<tr><td colspan="7" class="loading-cell">Загрузка…</td></tr>';
  const params = new URLSearchParams({
    page: String(state.vpnPage),
    page_size: "25",
    segment: state.vpnSegment,
  });
  if (state.vpnQuery) params.set("q", state.vpnQuery);
  try {
    const data = await api(`/api/vpn/users?${params}`);
    state.vpnPage = data.page;
    state.vpnPages = data.pages;
    byId("vpn-users-body").innerHTML = vpnUsersMarkup(data.users);
    byId("vpn-users-count").textContent =
      `${formatNumber.format(data.total)} записей`;
    byId("vpn-users-page-label").textContent =
      `Страница ${data.page} из ${data.pages}`;
    byId("vpn-pagination-label").textContent = `${data.page} / ${data.pages}`;
    byId("vpn-previous-page").disabled = data.page <= 1;
    byId("vpn-next-page").disabled = data.page >= data.pages;
  } catch (error) {
    byId("vpn-users-body").innerHTML =
      `<tr><td colspan="7" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
    showToast(error.message, true);
  }
}

function detail(label, value) {
  return `<div class="detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function activityMarkup(items, kind) {
  if (!items?.length) return '<div class="empty-list">Записей пока нет</div>';
  return items.map((item) => {
    const isPayment = kind === "payment";
    const title = isPayment
      ? `${item.plan_name || "Тариф"} · ${item.status}`
      : `${item.model_name || item.generation_type} · ${item.status}`;
    let subtitle = `${coinLabel(item.coins_charged)} · ${item.generation_type}`;
    if (isPayment && item.provider === "telegram_stars") {
      subtitle = "Telegram Stars";
    } else if (isPayment && item.provider === "mock") {
      subtitle = "Тестовая запись";
    } else if (isPayment) {
      subtitle = `${formatMoney.format((item.amount_rub || 0) - (item.discount_rub || 0))} · ${item.provider}`;
    }
    return `
      <div class="activity-item">
        <div>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(subtitle)}</span>
        </div>
        <strong>${escapeHtml(asDate(item.paid_at || item.created_at))}</strong>
      </div>
    `;
  }).join("");
}

function drawerMarkup(user) {
  const subscription = user.subscription || {};
  const telegramUrl = user.username
    ? `https://t.me/${encodeURIComponent(user.username)}`
    : `tg://user?id=${encodeURIComponent(user.telegram_id)}`;
  const segment = user.is_admin
    ? "Администратор"
    : user.has_paid
      ? "Плативший"
      : user.has_trial
        ? "Бесплатный доступ"
        : "Без доступа";
  const subscriptionText = subscription.id
    ? `${subscription.plan_name} · ${subscription.status}`
    : "Нет";
  const paidText = `${user.payments?.paid_count || 0} · ${formatMoney.format(user.payments?.paid_amount_rub || 0)}`;
  const starsText = user.payments?.stars_paid_count
    ? ` · ${user.payments.stars_paid_count} через Stars`
    : "";
  const management = state.canManage ? `
    <section class="drawer-section">
      <h3>Управление</h3>
      <div class="action-row">
        <button class="button ${user.is_blocked ? "primary" : "danger"}" id="block-user" type="button">
          ${user.is_blocked ? "Разблокировать" : "Заблокировать"}
        </button>
      </div>
    </section>
    <section class="drawer-section">
      <h3>Начислить коины</h3>
      <form class="credit-form" id="credit-form">
        <input id="credit-amount" type="number" min="1" max="100000" step="1" placeholder="Количество" required>
        <button class="button primary" type="submit">Начислить</button>
      </form>
    </section>
  ` : "";
  return `
    <div class="profile-head">
      <div>
        <h2>${escapeHtml(userName(user))}</h2>
        <p>${escapeHtml(segment)} · внутренний ID ${escapeHtml(user.id)}</p>
      </div>
      <a class="profile-link" href="${escapeHtml(telegramUrl)}" target="_blank" rel="noreferrer">Открыть Telegram ↗</a>
    </div>
    <div class="detail-grid">
      ${detail("Дата регистрации", asDate(user.created_at))}
      ${detail("Последняя активность в боте", asDate(user.last_seen_at))}
      ${detail("Тариф", subscriptionText)}
      ${detail("Баланс", coinLabel(subscription.coins_balance_cache || 0))}
      ${detail("Оплачено", `${paidText}${starsText}`)}
      ${detail("Генерации", `${user.generations?.total || 0} · потрачено ${coinLabel(user.generations?.spent_coins || 0)}`)}
      ${detail("Приглашено", String(user.invited_count || 0))}
      ${detail("Статус", user.is_blocked ? "Заблокирован" : "Активен")}
    </div>
    ${management}
    <section class="drawer-section">
      <h3>Последние платежи</h3>
      <div class="activity-list">${activityMarkup(user.recent_payments, "payment")}</div>
    </section>
    <section class="drawer-section">
      <h3>Последние генерации</h3>
      <div class="activity-list">${activityMarkup(user.recent_generations, "generation")}</div>
    </section>
  `;
}

function vpnActivityMarkup(items, kind) {
  if (!items?.length) return '<div class="empty-list">Записей пока нет</div>';
  return items.map((item) => {
    if (kind === "job") {
      return `
        <div class="activity-item">
          <div>
            <strong>${escapeHtml(`${item.operation} · ${item.status}`)}</strong>
            <span>${escapeHtml(item.server_name || "VPN-сервер")}${item.last_error ? ` · ${escapeHtml(item.last_error)}` : ""}</span>
          </div>
          <strong>${escapeHtml(asDate(item.completed_at || item.created_at))}</strong>
        </div>
      `;
    }
    return `
      <div class="activity-item">
        <div>
          <strong>${escapeHtml(`${item.plan_name || "VPN"} · ${item.status}`)}</strong>
          <span>${escapeHtml(`${formatMoney.format(item.amount_rub || 0)} · ${item.provider}`)}</span>
        </div>
        <strong>${escapeHtml(asDate(item.paid_at || item.created_at))}</strong>
      </div>
    `;
  }).join("");
}

function vpnDrawerMarkup(user) {
  const subscription = user.subscription || {};
  const trial = user.trial || {};
  const payments = user.payments || {};
  const telegramUrl = user.username
    ? `https://t.me/${encodeURIComponent(user.username)}`
    : `tg://user?id=${encodeURIComponent(user.telegram_id)}`;
  const statusLabels = {
    active: "Активна",
    provisioning: "Подключается",
    expired: "Истекла",
    disabled: "Отключена",
    error: "Ошибка",
  };
  const planName = subscription.plan_name
    || (subscription.kind === "trial" ? "3 дня бесплатно" : "—");
  const paid = `${payments.paid_count || 0} · ${formatMoney.format(payments.paid_amount_rub || 0)}`;
  return `
    <div class="profile-head">
      <div>
        <h2>${escapeHtml(userName(user))}</h2>
        <p>VPN-пользователь · внутренний ID ${escapeHtml(user.id)}</p>
      </div>
      <a class="profile-link" href="${escapeHtml(telegramUrl)}" target="_blank" rel="noreferrer">Открыть Telegram ↗</a>
    </div>
    <div class="detail-grid">
      ${detail("Тариф", planName)}
      ${detail("Статус", statusLabels[subscription.status] || subscription.status || "Нет подписки")}
      ${detail("Начало", asDate(subscription.starts_at))}
      ${detail("Окончание", asDate(subscription.ends_at))}
      ${detail("Оплачено", paid)}
      ${detail("Пробный период", trial.id ? "Использован" : "Не использован")}
      ${detail("Лимит устройств", subscription.max_devices ? String(subscription.max_devices) : "—")}
      ${detail("Синхронизация", asDate(subscription.last_synced_at))}
    </div>
    ${subscription.last_error ? `
      <section class="drawer-section">
        <h3>Ошибка подписки</h3>
        <div class="empty-list">${escapeHtml(subscription.last_error)}</div>
      </section>
    ` : ""}
    <section class="drawer-section">
      <h3>Последние VPN-платежи</h3>
      <div class="activity-list">${vpnActivityMarkup(user.recent_payments, "payment")}</div>
    </section>
    <section class="drawer-section">
      <h3>Выдача ключа по серверам</h3>
      <div class="activity-list">${vpnActivityMarkup(user.recent_jobs, "job")}</div>
    </section>
  `;
}

async function openUser(userId) {
  state.selectedUserId = Number(userId);
  state.selectedUserKind = "ai";
  const drawer = byId("user-drawer");
  byId("drawer-title").textContent = `#${state.selectedUserId}`;
  byId("drawer-content").innerHTML = '<div class="loading-cell">Загрузка…</div>';
  byId("drawer-backdrop").hidden = false;
  drawer.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  try {
    const user = await api(`/api/users/${state.selectedUserId}`);
    byId("drawer-title").textContent = userName(user);
    byId("drawer-content").innerHTML = drawerMarkup(user);
    bindDrawerActions(user);
  } catch (error) {
    byId("drawer-content").innerHTML = `<div class="empty-list">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  }
}

async function openVpnUser(userId) {
  state.selectedUserId = Number(userId);
  state.selectedUserKind = "vpn";
  const drawer = byId("user-drawer");
  byId("drawer-title").textContent = `#${state.selectedUserId}`;
  byId("drawer-content").innerHTML = '<div class="loading-cell">Загрузка…</div>';
  byId("drawer-backdrop").hidden = false;
  drawer.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  try {
    const user = await api(`/api/vpn/users/${state.selectedUserId}`);
    byId("drawer-title").textContent = userName(user);
    byId("drawer-content").innerHTML = vpnDrawerMarkup(user);
  } catch (error) {
    byId("drawer-content").innerHTML =
      `<div class="empty-list">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  }
}

function closeDrawer() {
  byId("user-drawer").classList.remove("is-open");
  byId("user-drawer").setAttribute("aria-hidden", "true");
  byId("drawer-backdrop").hidden = true;
  state.selectedUserId = null;
  state.selectedUserKind = null;
}

function bindDrawerActions(user) {
  const blockButton = byId("block-user");
  if (blockButton) {
    blockButton.addEventListener("click", async () => {
      const nextBlocked = !Boolean(user.is_blocked);
      const label = nextBlocked ? "заблокировать" : "разблокировать";
      if (!window.confirm(`Точно ${label} пользователя?`)) return;
      blockButton.disabled = true;
      try {
        const result = await api(`/api/users/${user.id}/blocked`, {
          method: "POST",
          body: JSON.stringify({ blocked: nextBlocked }),
        });
        showToast(nextBlocked ? "Пользователь заблокирован" : "Пользователь разблокирован");
        byId("drawer-content").innerHTML = drawerMarkup(result.user);
        bindDrawerActions(result.user);
        loadUsers();
        loadStats();
      } catch (error) {
        showToast(error.message, true);
        blockButton.disabled = false;
      }
    });
  }

  const creditForm = byId("credit-form");
  if (creditForm) {
    creditForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const amount = Number(byId("credit-amount").value);
      if (!Number.isInteger(amount) || amount <= 0) {
        showToast("Введите положительное целое число", true);
        return;
      }
      const submit = creditForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        const result = await api(`/api/users/${user.id}/credit`, {
          method: "POST",
          body: JSON.stringify({ amount }),
        });
        showToast(`Начислено ${coinLabel(amount)}`);
        byId("drawer-content").innerHTML = drawerMarkup(result.user);
        bindDrawerActions(result.user);
        loadUsers();
        loadStats();
      } catch (error) {
        showToast(error.message, true);
        submit.disabled = false;
      }
    });
  }
}

async function refreshAll() {
  const button = byId("refresh-button");
  button.disabled = true;
  try {
    await loadStatus();
    if (state.product === "vpn") {
      await loadVpnStats();
    } else {
      await loadStats();
    }
    if (state.view === "users") await loadUsers();
    if (state.view === "vpn-users") await loadVpnUsers();
    if (state.selectedUserId && state.selectedUserKind === "vpn") {
      await openVpnUser(state.selectedUserId);
    } else if (state.selectedUserId) {
      await openUser(state.selectedUserId);
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll(".product-card").forEach((button) => {
  button.addEventListener("click", () => setProduct(button.dataset.product));
});
document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});
document.querySelectorAll("[data-open-users]").forEach((button) => {
  button.addEventListener("click", () => setView("users"));
});
document.querySelectorAll("[data-open-vpn-users]").forEach((button) => {
  button.addEventListener("click", () => setView("vpn-users"));
});
document.querySelectorAll(".segment[data-segment]").forEach((button) => {
  button.addEventListener("click", () => {
    state.segment = button.dataset.segment;
    state.page = 1;
    document.querySelectorAll(".segment").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    loadUsers();
  });
});
document.querySelectorAll(".vpn-segment").forEach((button) => {
  button.addEventListener("click", () => {
    state.vpnSegment = button.dataset.vpnSegment;
    state.vpnPage = 1;
    document.querySelectorAll(".vpn-segment").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    loadVpnUsers();
  });
});

let searchTimer;
byId("user-search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = event.target.value.trim();
    state.page = 1;
    loadUsers();
  }, 250);
});
let vpnSearchTimer;
byId("vpn-user-search").addEventListener("input", (event) => {
  clearTimeout(vpnSearchTimer);
  vpnSearchTimer = setTimeout(() => {
    state.vpnQuery = event.target.value.trim();
    state.vpnPage = 1;
    loadVpnUsers();
  }, 250);
});
byId("previous-page").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadUsers();
  }
});
byId("next-page").addEventListener("click", () => {
  if (state.page < state.pages) {
    state.page += 1;
    loadUsers();
  }
});
byId("vpn-previous-page").addEventListener("click", () => {
  if (state.vpnPage > 1) {
    state.vpnPage -= 1;
    loadVpnUsers();
  }
});
byId("vpn-next-page").addEventListener("click", () => {
  if (state.vpnPage < state.vpnPages) {
    state.vpnPage += 1;
    loadVpnUsers();
  }
});
byId("users-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-user-id]");
  if (row) openUser(row.dataset.userId);
});
byId("users-body").addEventListener("keydown", (event) => {
  const row = event.target.closest("tr[data-user-id]");
  if (row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openUser(row.dataset.userId);
  }
});
byId("vpn-users-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-vpn-user-id]");
  if (row) openVpnUser(row.dataset.vpnUserId);
});
byId("vpn-users-body").addEventListener("keydown", (event) => {
  const row = event.target.closest("tr[data-vpn-user-id]");
  if (row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openVpnUser(row.dataset.vpnUserId);
  }
});
byId("drawer-close").addEventListener("click", closeDrawer);
byId("drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});
byId("refresh-button").addEventListener("click", refreshAll);
byId("maintenance-toggle").addEventListener("click", async () => {
  if (!state.canManage) return;
  const toggle = byId("maintenance-toggle");
  toggle.disabled = true;
  try {
    const result = await api("/api/maintenance", {
      method: "POST",
      body: JSON.stringify({ active: !state.maintenance }),
    });
    setMaintenance(result.maintenance_active);
    showToast(result.maintenance_active ? "Техработы включены" : "Техработы выключены");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});

setProduct("ai");
refreshAll();
