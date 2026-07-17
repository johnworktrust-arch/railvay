ALTER TABLE referral_transactions
    ADD COLUMN vpn_payment_provider TEXT;

ALTER TABLE referral_transactions
    ADD COLUMN vpn_payment_id INTEGER;

ALTER TABLE referral_transactions
    ADD COLUMN vpn_payment_external_id TEXT;

-- VPN payments live in their own table.  Keep this source deliberately free
-- of the legacy payments(id) foreign key while still making each reward and
-- each chargeback reversal independently idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_tx_vpn_payment_type
    ON referral_transactions(vpn_payment_provider, vpn_payment_id, type)
    WHERE vpn_payment_provider IS NOT NULL
      AND vpn_payment_id IS NOT NULL
      AND type IN ('credit', 'adjustment');
