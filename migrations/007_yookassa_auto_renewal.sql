ALTER TABLE subscriptions
    ADD COLUMN yookassa_payment_method_id TEXT;

ALTER TABLE subscriptions
    ADD COLUMN auto_renew_last_attempt_at TEXT;

ALTER TABLE subscriptions
    ADD COLUMN auto_renew_last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_subscriptions_auto_renew_due
    ON subscriptions(auto_renew, status, ends_at);
