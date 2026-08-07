PRAGMA foreign_keys = OFF;

CREATE TEMP TABLE _assert_no_music_generations (
    message TEXT NOT NULL CHECK (message <> 'music generation history exists')
);

INSERT INTO _assert_no_music_generations (message)
SELECT 'music generation history exists'
WHERE EXISTS (
    SELECT 1 FROM generations WHERE generation_type = 'music'
);

DROP TABLE _assert_no_music_generations;

DELETE FROM model_prices
WHERE generation_type = 'music'
  AND id NOT IN (
      SELECT model_price_id FROM generations WHERE model_price_id IS NOT NULL
  );

CREATE TEMP TABLE _assert_no_music_model_prices (
    message TEXT NOT NULL CHECK (message <> 'music model price is linked to generations')
);

INSERT INTO _assert_no_music_model_prices (message)
SELECT 'music model price is linked to generations'
WHERE EXISTS (
    SELECT 1 FROM model_prices WHERE generation_type = 'music'
);

DROP TABLE _assert_no_music_model_prices;

CREATE TABLE model_prices_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    generation_type TEXT NOT NULL CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo')),
    coins_cost INTEGER NOT NULL CHECK (coins_cost > 0),
    is_active INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, model_key)
);

INSERT INTO model_prices_new (
    id, provider, model_key, display_name, generation_type, coins_cost,
    is_active, config, created_at, updated_at
)
SELECT
    id, provider, model_key, display_name, generation_type, coins_cost,
    is_active, config, created_at, updated_at
FROM model_prices;

DROP TABLE model_prices;
ALTER TABLE model_prices_new RENAME TO model_prices;

CREATE TABLE generations_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    model_price_id INTEGER NOT NULL REFERENCES model_prices(id),
    generation_type TEXT NOT NULL CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo')),
    provider TEXT NOT NULL,
    provider_job_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    coins_reserved INTEGER NOT NULL DEFAULT 0 CHECK (coins_reserved >= 0),
    coins_charged INTEGER NOT NULL DEFAULT 0 CHECK (coins_charged >= 0),
    provider_cost_amount NUMERIC,
    provider_cost_currency TEXT,
    duration_seconds INTEGER,
    prompt TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

INSERT INTO generations_new (
    id, user_id, subscription_id, model_price_id, generation_type, provider,
    provider_job_id, status, coins_reserved, coins_charged, provider_cost_amount,
    provider_cost_currency, duration_seconds, prompt, result, error_message,
    created_at, completed_at
)
SELECT
    id, user_id, subscription_id, model_price_id, generation_type, provider,
    provider_job_id, status, coins_reserved, coins_charged, provider_cost_amount,
    provider_cost_currency, duration_seconds, prompt, result, error_message,
    created_at, completed_at
FROM generations;

DROP TABLE generations;
ALTER TABLE generations_new RENAME TO generations;

CREATE INDEX IF NOT EXISTS idx_generations_user_created
    ON generations(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_generations_status
    ON generations(status);

CREATE INDEX IF NOT EXISTS idx_generations_provider_job
    ON generations(provider, provider_job_id);

PRAGMA foreign_key_check;
PRAGMA foreign_keys = ON;
