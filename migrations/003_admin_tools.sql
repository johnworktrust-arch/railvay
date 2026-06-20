CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_user
    ON admin_users(user_id);

CREATE TABLE IF NOT EXISTS admin_action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id),
    target_user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_action_logs_admin_created
    ON admin_action_logs(admin_user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_admin_action_logs_target_created
    ON admin_action_logs(target_user_id, created_at);
