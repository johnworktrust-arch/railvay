-- Some production databases received the original promocode-redemptions
-- table before the one-time discount flag was introduced.  `021` is already
-- recorded there, so repair the schema in a new, idempotent migration.
ALTER TABLE vpn_promocode_redemptions
    ADD COLUMN IF NOT EXISTS is_used BOOLEAN NOT NULL DEFAULT FALSE;
