CREATE TABLE IF NOT EXISTS text_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    model_price_id INTEGER NOT NULL REFERENCES model_prices(id),
    title TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_text_chats_user_model_active
    ON text_chats(user_id, model_price_id, is_active);
