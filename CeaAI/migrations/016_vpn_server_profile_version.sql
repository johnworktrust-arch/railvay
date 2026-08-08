ALTER TABLE vpn_servers
    ADD COLUMN current_profile_version TEXT;
ALTER TABLE vpn_servers
    ADD COLUMN current_worker_epoch TEXT;
