CREATE TABLE IF NOT EXISTS vpn_reengagement_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id INTEGER NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('inactive_discount', 'expired_notice', 'expired_discount')),
    campaign_day TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL,
    sent_at TEXT NULL,
    UNIQUE(user_id, kind, campaign_day)
);
