UPDATE vpn_plans SET max_devices = 2 WHERE max_devices = 1;
ALTER TABLE vpn_subscriptions ADD COLUMN IF NOT EXISTS extra_devices INTEGER NOT NULL DEFAULT 0;
