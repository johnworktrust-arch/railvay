CREATE TABLE IF NOT EXISTS referral_transactions (
    id BIGSERIAL PRIMARY KEY,
    referrer_user_id BIGINT NOT NULL REFERENCES users(id),
    referred_user_id BIGINT NOT NULL REFERENCES users(id),
    payment_id BIGINT REFERENCES payments(id),
    amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks <> 0),
    rate_percent INTEGER NOT NULL CHECK (rate_percent >= 0),
    type TEXT NOT NULL CHECK (type IN ('credit', 'withdrawal', 'adjustment')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'cancelled', 'failed')),
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_referral_transactions_referrer_created
    ON referral_transactions(referrer_user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_referral_transactions_referred_created
    ON referral_transactions(referred_user_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_transactions_credit_payment
    ON referral_transactions(payment_id)
    WHERE type = 'credit' AND payment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS referral_payout_settings (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id),
    withdrawal_method TEXT NOT NULL DEFAULT '',
    requisites TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
