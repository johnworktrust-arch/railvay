CREATE TABLE vpn_purchase_notification_start (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
INSERT INTO vpn_purchase_notification_start VALUES (1, to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00');
CREATE TABLE vpn_purchase_notifications (
 payment_id INTEGER NOT NULL REFERENCES vpn_payments(id),
 chat_id BIGINT NOT NULL,
 lease_until TEXT NOT NULL,
 sent_at TEXT,
 PRIMARY KEY (payment_id, chat_id)
);
