from datetime import datetime, timedelta, timezone
from html import escape
import logging

from ceavpn.time_utils import utcnow


def purchase_message(payment):
    paid_at = payment['paid_at']
    if isinstance(paid_at, str):
        paid_at = datetime.fromisoformat(paid_at)
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    stamp = paid_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M:%S')
    username = payment.get('username')
    buyer = '@' + escape(username.lstrip('@')) if username else 'Без username'
    unit = '⭐️' if payment.get('currency') == 'XTR' else 'руб'
    return (
        '🛍 <b>Новая покупка</b>\n\n'
        f'⭐️ <b>Сумма:</b> {int(payment["amount_rub"])} {unit}\n'
        f'👤 <b>Покупатель:</b> {buyer}\n'
        f'🆔 <b>Telegram ID:</b> {int(payment["telegram_id"])}\n'
        f'🕒 <b>Время:</b> {stamp} MSK'
    )


async def notify_new_purchases(services, bot):
    if bot is None:
        return
    db = services.vpn.db
    settings = services.settings
    targets = set(settings.admin_telegram_ids) | set(settings.vpn_admin_demo_telegram_ids)
    with db.transaction() as conn:
        for username in settings.admin_telegram_usernames:
            row = conn.execute('SELECT telegram_id FROM users WHERE lower(username) = ?', (username.lstrip('@').lower(),)).fetchone()
            if row:
                targets.add(int(row['telegram_id']))
        start = conn.execute('SELECT started_at FROM vpn_purchase_notification_start WHERE id = 1').fetchone()['started_at']
    for target in targets:
        with db.transaction() as conn:
            payments = [dict(r) for r in conn.execute(
                """SELECT p.*, u.username, u.telegram_id FROM vpn_payments p
                JOIN users u ON u.id = p.user_id
                LEFT JOIN vpn_purchase_notifications n ON n.payment_id = p.id AND n.chat_id = ?
                WHERE p.status = 'paid' AND p.provider <> 'admin_demo' AND p.paid_at >= ?
                  AND n.sent_at IS NULL
                ORDER BY p.id LIMIT 100""", (target, start)).fetchall()]
        for payment in payments:
            now = utcnow()
            with db.transaction() as conn:
                claim = conn.execute(
                    """INSERT INTO vpn_purchase_notifications (payment_id, chat_id, lease_until)
                    VALUES (?, ?, ?) ON CONFLICT(payment_id, chat_id) DO UPDATE
                    SET lease_until = excluded.lease_until
                    WHERE vpn_purchase_notifications.sent_at IS NULL
                      AND vpn_purchase_notifications.lease_until < ?
                    RETURNING payment_id""",
                    (payment['id'], target, (now + timedelta(minutes=5)).isoformat(), now.isoformat()),
                ).fetchone()
            if not claim:
                continue
            try:
                await bot.send_message(chat_id=target, text=purchase_message(payment), parse_mode='HTML')
                with db.transaction() as conn:
                    conn.execute('UPDATE vpn_purchase_notifications SET sent_at = ? WHERE payment_id = ? AND chat_id = ?', (utcnow().isoformat(), payment['id'], target))
            except Exception:
                logging.exception('VPN admin purchase notification failed for payment %s', payment['id'])
