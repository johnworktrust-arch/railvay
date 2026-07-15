ALTER TABLE vpn_servers ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE vpn_servers
    ADD COLUMN IF NOT EXISTS subscription_base_url TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_servers_worker_id
    ON vpn_servers(worker_id)
    WHERE worker_id IS NOT NULL AND worker_id <> '';

CREATE TABLE IF NOT EXISTS vpn_worker_nonces (
    worker_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (worker_id, nonce)
);

CREATE INDEX IF NOT EXISTS idx_vpn_worker_nonces_expires
    ON vpn_worker_nonces(expires_at);
