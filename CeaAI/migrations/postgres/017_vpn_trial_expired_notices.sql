ALTER TABLE vpn_trial_claims
    ADD COLUMN IF NOT EXISTS expired_notice_claimed_at TIMESTAMPTZ;

ALTER TABLE vpn_trial_claims
    ADD COLUMN IF NOT EXISTS expired_notice_sent_at TIMESTAMPTZ;

-- Do not send a one-time launch burst for trials that had already expired
-- before this notification type existed. New expirations remain NULL and
-- can be retried without an arbitrary age cutoff after temporary outages.
UPDATE vpn_trial_claims
SET expired_notice_sent_at = NOW()
WHERE expired_notice_sent_at IS NULL
  AND EXISTS (
        SELECT 1
        FROM vpn_subscriptions subscription
        WHERE subscription.id = vpn_trial_claims.subscription_id
          AND subscription.ends_at <= NOW()
  );

CREATE INDEX IF NOT EXISTS idx_vpn_trial_claims_expired_notice
    ON vpn_trial_claims(
        status, expired_notice_sent_at, expired_notice_claimed_at
    );
