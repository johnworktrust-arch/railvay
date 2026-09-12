CREATE TABLE vpn_purchase_notification_start (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
INSERT INTO vpn_purchase_notification_start VALUES (1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
CREATE TABLE vpn_purchase_notifications (
 payment_id INTEGER NOT NULL REFERENCES vpn_payments(id),
 chat_id BIGINT NOT NULL,
 lease_until TEXT NOT NULL,
 sent_at TEXT,
 PRIMARY KEY (payment_id, chat_id)
);
