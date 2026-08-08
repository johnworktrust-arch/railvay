ALTER TABLE vpn_plans
    ALTER COLUMN max_devices SET DEFAULT 1;

UPDATE vpn_plans
SET max_devices = 1,
    updated_at = CURRENT_TIMESTAMP;
