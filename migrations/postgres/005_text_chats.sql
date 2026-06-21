CREATE TABLE IF NOT EXISTS text_chats (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    model_price_id BIGINT NOT NULL REFERENCES model_prices(id),
    title TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_text_chats_user_model_active
    ON text_chats(user_id, model_price_id, is_active);
