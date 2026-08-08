ALTER TABLE vpn_subscriptions
    ADD COLUMN provider_uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_subscriptions_provider_uuid
    ON vpn_subscriptions(provider_uuid)
    WHERE provider_uuid IS NOT NULL AND provider_uuid <> '';

ALTER TABLE vpn_provisioning_jobs
    ADD COLUMN server_id INTEGER REFERENCES vpn_servers(id);

UPDATE vpn_provisioning_jobs
SET server_id = (
    SELECT subscription.server_id
    FROM vpn_subscriptions subscription
    WHERE subscription.id = vpn_provisioning_jobs.subscription_id
)
WHERE server_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_vpn_provisioning_jobs_server_due
    ON vpn_provisioning_jobs(
        server_id, status, next_attempt_at, lease_expires_at
    );
