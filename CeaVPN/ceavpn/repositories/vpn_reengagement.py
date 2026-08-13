from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceavpn.repositories.base import rows_to_dicts


class VpnReengagementRepository:
    def claim_due(self, conn: sqlite3.Connection, *, now: str, discount_due: str, day: str) -> List[Dict[str, Any]]:
        candidates = conn.execute(
            """
            SELECT u.id AS user_id, u.telegram_id, NULL AS subscription_id, 'inactive_discount' AS kind
            FROM users u
            WHERE u.created_at <= ?
              AND NOT EXISTS (SELECT 1 FROM vpn_subscriptions s WHERE s.user_id = u.id)
              AND NOT EXISTS (SELECT 1 FROM vpn_reengagement_messages m WHERE m.user_id=u.id AND m.kind='inactive_discount' AND m.campaign_day=?)
            UNION ALL
            SELECT s.user_id, u.telegram_id, s.id, CASE WHEN s.ends_at <= ? THEN 'expired_discount' ELSE 'expired_notice' END
            FROM vpn_subscriptions s JOIN users u ON u.id=s.user_id
            WHERE s.ends_at <= ?
              AND s.id=(SELECT x.id FROM vpn_subscriptions x WHERE x.user_id=s.user_id ORDER BY x.ends_at DESC, x.id DESC LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM vpn_subscriptions paid WHERE paid.user_id=s.user_id AND paid.billing_kind='paid' AND paid.ends_at>s.ends_at)
              AND NOT EXISTS (SELECT 1 FROM vpn_reengagement_messages m WHERE m.user_id=s.user_id AND m.kind=CASE WHEN s.ends_at <= ? THEN 'expired_discount' ELSE 'expired_notice' END AND m.campaign_day='')
            """,
            (now, day, discount_due, now, discount_due),
        ).fetchall()
        claimed=[]
        for row in candidates:
            item=dict(row); campaign_day=day if item['kind']=='inactive_discount' else ''
            try:
                conn.execute("INSERT INTO vpn_reengagement_messages (user_id, subscription_id, kind, campaign_day, claimed_at) VALUES (?, ?, ?, ?, ?)", (item['user_id'], item['subscription_id'], item['kind'], campaign_day, now))
            except Exception:
                continue
            claimed.append(item)
        return claimed
