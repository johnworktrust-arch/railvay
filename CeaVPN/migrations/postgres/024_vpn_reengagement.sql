CREATE TABLE IF NOT EXISTS vpn_reengagement_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id BIGINT NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL CHECK (kind IN ('inactive_discount', 'expired_notice', 'expired_discount')),
    campaign_day TEXT NOT NULL DEFAULT '',
    claimed_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ NULL,
    UNIQUE(user_id, kind, campaign_day)
);
