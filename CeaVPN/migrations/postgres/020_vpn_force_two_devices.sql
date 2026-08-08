-- Force all VPN plans to max_devices = 2 regardless of previous value
UPDATE vpn_plans SET max_devices = 2;
