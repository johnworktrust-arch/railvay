CREATE TABLE IF NOT EXISTS vpn_user_bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    admin_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lifted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_vpn_user_bans_active
    ON vpn_user_bans(is_active, updated_at);
