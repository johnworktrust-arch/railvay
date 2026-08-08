ALTER TABLE vpn_subscriptions
    ADD COLUMN IF NOT EXISTS provider_uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_subscriptions_provider_uuid
    ON vpn_subscriptions(provider_uuid)
    WHERE provider_uuid IS NOT NULL AND provider_uuid <> '';

ALTER TABLE vpn_provisioning_jobs
    ADD COLUMN IF NOT EXISTS server_id BIGINT REFERENCES vpn_servers(id);

UPDATE vpn_provisioning_jobs job
SET server_id = subscription.server_id
FROM vpn_subscriptions subscription
WHERE subscription.id = job.subscription_id
  AND job.server_id IS NULL;

ALTER TABLE vpn_provisioning_jobs
    ALTER COLUMN server_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_vpn_provisioning_jobs_server_due
    ON vpn_provisioning_jobs(
        server_id, status, next_attempt_at, lease_expires_at
    );
