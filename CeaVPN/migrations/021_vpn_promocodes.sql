CREATE TABLE IF NOT EXISTS vpn_promocodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    reward_type TEXT NOT NULL CHECK (reward_type IN ('days', 'devices', 'plan')),
    reward_value INTEGER NOT NULL DEFAULT 0,
    target_user_id INTEGER NULL,
    max_uses INTEGER NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    starts_at TEXT NULL,
    expires_at TEXT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vpn_promocode_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promocode_id INTEGER NOT NULL REFERENCES vpn_promocodes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_summary TEXT NOT NULL DEFAULT '',
    redeemed_at TEXT NOT NULL,
    UNIQUE(promocode_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_vpn_promocodes_code ON vpn_promocodes(code);
CREATE INDEX IF NOT EXISTS idx_vpn_promocode_redemptions_user ON vpn_promocode_redemptions(user_id);
