CREATE TABLE IF NOT EXISTS vpn_subscription_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
    device_key TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    deactivated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subscription_id, device_key)
);

CREATE INDEX IF NOT EXISTS idx_vpn_subscription_devices_active
    ON vpn_subscription_devices(subscription_id, deactivated_at, first_seen_at);
