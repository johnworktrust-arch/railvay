ALTER TABLE vpn_trial_claims
    ADD COLUMN expiry_reminder_claimed_at TEXT;

ALTER TABLE vpn_trial_claims
    ADD COLUMN expiry_reminder_sent_at TEXT;

CREATE INDEX IF NOT EXISTS idx_vpn_trial_claims_expiry_reminder
    ON vpn_trial_claims(
        status, expiry_reminder_sent_at, expiry_reminder_claimed_at
    );
