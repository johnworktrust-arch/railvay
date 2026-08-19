-- An extra-device order changes the device limit without extending the
-- subscription, so its duration snapshot is intentionally zero days.
ALTER TABLE vpn_payments
    DROP CONSTRAINT IF EXISTS vpn_payments_duration_days_check;

ALTER TABLE vpn_payments
    ADD CONSTRAINT vpn_payments_duration_days_check
    CHECK (duration_days >= 0);
