-- An extra-device order changes the device limit without extending the
-- subscription, so its duration snapshot is intentionally zero days.
CREATE TABLE vpn_payments_rebuild (
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
    duration_days INTEGER NOT NULL CHECK (duration_days >= 0),
    currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    payment_url TEXT,
    expires_at TEXT,
    UNIQUE(provider, external_id),
    CHECK (status <> 'paid' OR paid_at IS NOT NULL)
);

INSERT INTO vpn_payments_rebuild (
    id, user_id, vpn_plan_id, vpn_subscription_id, provider, external_id,
    payment_method, status, amount_rub, duration_days, currency, created_at,
    updated_at, paid_at, payment_url, expires_at
)
SELECT
    id, user_id, vpn_plan_id, vpn_subscription_id, provider, external_id,
    payment_method, status, amount_rub, duration_days, currency, created_at,
    updated_at, paid_at, payment_url, expires_at
FROM vpn_payments;

DROP TABLE vpn_payments;
ALTER TABLE vpn_payments_rebuild RENAME TO vpn_payments;

CREATE INDEX idx_vpn_payments_user_status
    ON vpn_payments(user_id, status, created_at);
CREATE INDEX idx_vpn_payments_subscription
    ON vpn_payments(vpn_subscription_id)
    WHERE vpn_subscription_id IS NOT NULL;
CREATE UNIQUE INDEX idx_vpn_payments_one_pending_admin_demo
    ON vpn_payments(user_id, vpn_plan_id, payment_method)
    WHERE provider = 'admin_demo' AND status = 'pending';
CREATE UNIQUE INDEX idx_vpn_payments_one_pending_platega
    ON vpn_payments(user_id, vpn_plan_id, payment_method)
    WHERE provider = 'platega' AND status = 'pending';
CREATE INDEX idx_vpn_payments_platega_pending_reconciliation
    ON vpn_payments(updated_at, id)
    WHERE provider = 'platega' AND status = 'pending' AND payment_url IS NOT NULL;
CREATE INDEX idx_vpn_payments_platega_failed_reconciliation
    ON vpn_payments(updated_at, id)
    WHERE provider = 'platega' AND status = 'failed' AND payment_url IS NOT NULL;
CREATE INDEX idx_vpn_payments_platega_paid_reconciliation
    ON vpn_payments(paid_at, id)
    WHERE provider = 'platega' AND status = 'paid' AND payment_url IS NOT NULL;
