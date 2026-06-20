CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT,
    is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    referral_code TEXT NOT NULL UNIQUE,
    referred_by_user_id BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    price_rub INTEGER NOT NULL CHECK (price_rub >= 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    coins_amount INTEGER NOT NULL CHECK (coins_amount >= 0),
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS promocodes (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('discount', 'bonus_coins', 'free_trial')),
    discount_rub INTEGER NOT NULL DEFAULT 0,
    bonus_coins INTEGER NOT NULL DEFAULT 0,
    usage_limit INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    plan_id BIGINT NOT NULL REFERENCES plans(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
    coins_balance_cache INTEGER NOT NULL DEFAULT 0,
    auto_renew BOOLEAN NOT NULL DEFAULT FALSE,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_one_active
    ON subscriptions(user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status
    ON subscriptions(user_id, status);

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    plan_id BIGINT NOT NULL REFERENCES plans(id),
    subscription_id BIGINT REFERENCES subscriptions(id),
    promocode_id BIGINT REFERENCES promocodes(id),
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')),
    amount_rub INTEGER NOT NULL CHECK (amount_rub >= 0),
    discount_rub INTEGER NOT NULL DEFAULT 0,
    payment_url TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    UNIQUE(provider, external_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_user_status
    ON payments(user_id, status);

CREATE TABLE IF NOT EXISTS webhook_logs (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('received', 'processed', 'ignored', 'failed')),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    UNIQUE(provider, external_id, event_type)
);

CREATE TABLE IF NOT EXISTS model_prices (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    model_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    generation_type TEXT NOT NULL CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo')),
    coins_cost INTEGER NOT NULL CHECK (coins_cost > 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(provider, model_key)
);

CREATE TABLE IF NOT EXISTS generations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    subscription_id BIGINT REFERENCES subscriptions(id),
    model_price_id BIGINT NOT NULL REFERENCES model_prices(id),
    generation_type TEXT NOT NULL CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo')),
    provider TEXT NOT NULL,
    provider_job_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    coins_reserved INTEGER NOT NULL DEFAULT 0 CHECK (coins_reserved >= 0),
    coins_charged INTEGER NOT NULL DEFAULT 0 CHECK (coins_charged >= 0),
    provider_cost_amount NUMERIC,
    provider_cost_currency TEXT,
    duration_seconds INTEGER,
    prompt JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_generations_user_created
    ON generations(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_generations_status
    ON generations(status);

CREATE INDEX IF NOT EXISTS idx_generations_provider_job
    ON generations(provider, provider_job_id);

CREATE TABLE IF NOT EXISTS coin_transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    subscription_id BIGINT REFERENCES subscriptions(id),
    payment_id BIGINT REFERENCES payments(id),
    generation_id BIGINT REFERENCES generations(id),
    amount INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('credit', 'reserve', 'debit', 'refund', 'manual_adjustment', 'promo_bonus', 'referral_bonus')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'cancelled', 'failed')),
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_coin_transactions_user_created
    ON coin_transactions(user_id, created_at);

CREATE TABLE IF NOT EXISTS promocode_redemptions (
    id BIGSERIAL PRIMARY KEY,
    promocode_id BIGINT NOT NULL REFERENCES promocodes(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    payment_id BIGINT REFERENCES payments(id),
    used_at TIMESTAMPTZ NOT NULL,
    UNIQUE(promocode_id, user_id)
);

CREATE TABLE IF NOT EXISTS bot_sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    state TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_sessions_user
    ON bot_sessions(user_id);

CREATE TABLE IF NOT EXISTS admin_users (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'support')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL
);
