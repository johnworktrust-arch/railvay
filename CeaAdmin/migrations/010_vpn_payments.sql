ALTER TABLE vpn_subscriptions
    ADD COLUMN billing_kind TEXT NOT NULL DEFAULT 'trial'
        CHECK (billing_kind IN ('trial', 'paid'));

UPDATE vpn_subscriptions
SET billing_kind = kind;

CREATE TABLE IF NOT EXISTS vpn_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    vpn_plan_id INTEGER NOT NULL REFERENCES vpn_plans(id),
    vpn_subscription_id INTEGER REFERENCES vpn_subscriptions(id),
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')
    ),
    amount_rub INTEGER NOT NULL CHECK (amount_rub >= 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    UNIQUE(provider, external_id),
    CHECK (status <> 'paid' OR paid_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_vpn_payments_user_status
    ON vpn_payments(user_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_vpn_payments_subscription
    ON vpn_payments(vpn_subscription_id)
    WHERE vpn_subscription_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_payments_one_pending_admin_demo
    ON vpn_payments(user_id, vpn_plan_id, payment_method)
    WHERE provider = 'admin_demo' AND status = 'pending';
