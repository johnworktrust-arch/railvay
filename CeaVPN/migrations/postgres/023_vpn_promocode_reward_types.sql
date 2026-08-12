ALTER TABLE vpn_promocodes
    DROP CONSTRAINT IF EXISTS vpn_promocodes_reward_type_check;

ALTER TABLE vpn_promocodes
    ADD CONSTRAINT vpn_promocodes_reward_type_check
    CHECK (reward_type IN ('days', 'devices', 'plan', 'discount_percent', 'discount_fixed'));
