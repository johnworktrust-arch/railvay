CREATE TABLE IF NOT EXISTS vpn_vip_users (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    granted_by INTEGER NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
