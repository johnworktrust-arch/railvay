ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS yookassa_payment_method_id TEXT;

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS auto_renew_last_attempt_at TIMESTAMPTZ;

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS auto_renew_last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_subscriptions_auto_renew_due
    ON subscriptions(auto_renew, status, ends_at);
