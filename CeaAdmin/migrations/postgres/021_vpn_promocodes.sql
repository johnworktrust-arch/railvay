CREATE TABLE IF NOT EXISTS vpn_promocodes (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(64) NOT NULL UNIQUE,
    reward_type VARCHAR(32) NOT NULL CHECK (reward_type IN ('days', 'devices', 'plan', 'discount_percent', 'discount_fixed')),
    reward_value INTEGER NOT NULL DEFAULT 0,
    target_user_id BIGINT NULL,
    max_uses INTEGER NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    starts_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS vpn_promocode_redemptions (
    id BIGSERIAL PRIMARY KEY,
    promocode_id BIGINT NOT NULL REFERENCES vpn_promocodes(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_summary TEXT NOT NULL DEFAULT '',
    is_used BOOLEAN NOT NULL DEFAULT FALSE,
    redeemed_at TIMESTAMPTZ NOT NULL,
    UNIQUE(promocode_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_vpn_promocodes_code ON vpn_promocodes(code);
CREATE INDEX IF NOT EXISTS idx_vpn_promocode_redemptions_user ON vpn_promocode_redemptions(user_id);
