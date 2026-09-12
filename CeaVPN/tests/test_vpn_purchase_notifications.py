import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ceavpn.database import Database
from ceavpn.vpn_purchase_notifications import notify_new_purchases, purchase_message


class PurchaseNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_new_real_purchases_are_sent_once(self):
        db = Database('sqlite:///:memory:')
        self.addCleanup(db.close)
        with db.transaction() as conn:
            conn.execute('CREATE TABLE users (id INTEGER, telegram_id INTEGER, username TEXT)')
            conn.execute('CREATE TABLE vpn_payments (id INTEGER, user_id INTEGER, status TEXT, provider TEXT, paid_at TEXT, amount_rub INTEGER, currency TEXT)')
            conn.execute('CREATE TABLE vpn_purchase_notification_start (id INTEGER, started_at TEXT)')
            conn.execute('CREATE TABLE vpn_purchase_notifications (payment_id INTEGER, chat_id INTEGER, lease_until TEXT, sent_at TEXT, PRIMARY KEY(payment_id, chat_id))')
            conn.execute("INSERT INTO users VALUES (1,8189022272,'folkvallei')")
            conn.execute("INSERT INTO vpn_purchase_notification_start VALUES (1,'2026-09-12T00:00:00+00:00')")
            for pid, status, provider, date in [
                (1, 'paid', 'platega', '2026-09-12T14:19:38+00:00'),
                (2, 'pending', 'platega', '2026-09-12T14:19:38+00:00'),
                (3, 'paid', 'admin_demo', '2026-09-12T14:19:38+00:00'),
                (4, 'paid', 'platega', '2026-09-01T14:19:38+00:00'),
            ]:
                conn.execute('INSERT INTO vpn_payments VALUES (?,1,?,?,?,200,?)', (pid, status, provider, date, 'RUB'))
        services = SimpleNamespace(vpn=SimpleNamespace(db=db), settings=SimpleNamespace(
            admin_telegram_ids=(123,), vpn_admin_demo_telegram_ids=(123,), admin_telegram_usernames=()))
        bot = SimpleNamespace(send_message=AsyncMock())
        await notify_new_purchases(services, bot)
        await notify_new_purchases(services, bot)
        bot.send_message.assert_awaited_once()
        self.assertIn('12.09.2026 17:19:38 MSK', bot.send_message.call_args.kwargs['text'])

    def test_stars_and_escaped_username(self):
        text = purchase_message(dict(paid_at='2026-09-12T14:19:38+00:00', username='<user>', amount_rub=139, currency='XTR', telegram_id=1))
        self.assertIn('139 ⭐️', text)
        self.assertIn('@&lt;user&gt;', text)
