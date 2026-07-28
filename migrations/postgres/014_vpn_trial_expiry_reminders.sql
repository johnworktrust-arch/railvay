ALTER TABLE vpn_trial_claims
    ADD COLUMN IF NOT EXISTS expiry_reminder_claimed_at TIMESTAMPTZ;

ALTER TABLE vpn_trial_claims
    ADD COLUMN IF NOT EXISTS expiry_reminder_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_vpn_trial_claims_expiry_reminder
    ON vpn_trial_claims(
        status, expiry_reminder_sent_at, expiry_reminder_claimed_at
    );
