CREATE TABLE IF NOT EXISTS vpn_user_bans (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    reason TEXT NOT NULL DEFAULT '',
    admin_user_id BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    lifted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_vpn_user_bans_active
    ON vpn_user_bans(is_active, updated_at);
