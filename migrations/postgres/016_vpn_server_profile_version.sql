ALTER TABLE vpn_servers
    ADD COLUMN IF NOT EXISTS current_profile_version TEXT;
ALTER TABLE vpn_servers
    ADD COLUMN IF NOT EXISTS current_worker_epoch TEXT;
