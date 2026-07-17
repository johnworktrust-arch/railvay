ALTER TABLE vpn_payments
    ADD COLUMN IF NOT EXISTS payment_url TEXT;

ALTER TABLE vpn_payments
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_payments_one_pending_platega
    ON vpn_payments(user_id, vpn_plan_id, payment_method)
    WHERE provider = 'platega' AND status = 'pending';

CREATE INDEX IF NOT EXISTS idx_vpn_payments_platega_pending_reconciliation
    ON vpn_payments(updated_at, id)
    WHERE provider = 'platega'
      AND status = 'pending'
      AND payment_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_vpn_payments_platega_failed_reconciliation
    ON vpn_payments(updated_at, id)
    WHERE provider = 'platega'
      AND status = 'failed'
      AND payment_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_vpn_payments_platega_paid_reconciliation
    ON vpn_payments(paid_at, id)
    WHERE provider = 'platega'
      AND status = 'paid'
      AND payment_url IS NOT NULL;
