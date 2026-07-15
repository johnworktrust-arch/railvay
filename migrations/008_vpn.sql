CREATE TABLE IF NOT EXISTS vpn_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    api_base_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_health_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vpn_servers_active
    ON vpn_servers(is_active, code);

CREATE TABLE IF NOT EXISTS vpn_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    price_rub INTEGER NOT NULL CHECK (price_rub >= 0),
    price_stars INTEGER NOT NULL CHECK (price_stars >= 0),
    max_devices INTEGER NOT NULL DEFAULT 3 CHECK (max_devices > 0),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vpn_plans_active_price
    ON vpn_plans(is_active, price_rub);

CREATE TABLE IF NOT EXISTS vpn_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    server_id INTEGER NOT NULL REFERENCES vpn_servers(id),
    plan_id INTEGER REFERENCES vpn_plans(id),
    kind TEXT NOT NULL CHECK (kind IN ('trial', 'paid')),
    status TEXT NOT NULL CHECK (
        status IN ('provisioning', 'active', 'expired', 'disabled', 'error')
    ),
    provider_username TEXT NOT NULL,
    subscription_url TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    last_synced_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(server_id, provider_username),
    UNIQUE(id, user_id, kind)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_subscriptions_one_live
    ON vpn_subscriptions(user_id)
    WHERE status IN ('provisioning', 'active');

CREATE INDEX IF NOT EXISTS idx_vpn_subscriptions_user_status
    ON vpn_subscriptions(user_id, status);

CREATE INDEX IF NOT EXISTS idx_vpn_subscriptions_status_ends
    ON vpn_subscriptions(status, ends_at);

CREATE TABLE IF NOT EXISTS vpn_trial_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id),
    subscription_kind TEXT NOT NULL DEFAULT 'trial'
        CHECK (subscription_kind = 'trial'),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'provisioned', 'failed')),
    claimed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id, user_id, subscription_kind)
        REFERENCES vpn_subscriptions(id, user_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_vpn_trial_claims_status
    ON vpn_trial_claims(status, claimed_at);

CREATE TABLE IF NOT EXISTS vpn_provisioning_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id),
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update', 'disable', 'sync')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_vpn_provisioning_jobs_due
    ON vpn_provisioning_jobs(status, next_attempt_at, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_vpn_provisioning_jobs_subscription
    ON vpn_provisioning_jobs(subscription_id, created_at);
