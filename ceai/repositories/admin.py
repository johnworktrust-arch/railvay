from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class AdminRepository:
    def get_admin_by_user_id(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT au.*, u.telegram_id, u.username
                FROM admin_users au
                JOIN users u ON u.id = au.user_id
                WHERE au.user_id = ?
                """,
                (user_id,),
            ).fetchone()
        )

    def upsert_admin(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        role: str,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO admin_users (user_id, role, is_active, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role = excluded.role,
                is_active = excluded.is_active
            RETURNING id
            """,
            (user_id, role, bool(is_active), now),
        )
        row = cursor.fetchone()
        admin = self.get_admin_by_id(conn, int(row["id"]))
        if admin is None:
            raise RuntimeError("Could not upsert admin user")
        return admin

    def get_admin_by_id(
        self, conn: sqlite3.Connection, admin_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT au.*, u.telegram_id, u.username
                FROM admin_users au
                JOIN users u ON u.id = au.user_id
                WHERE au.id = ?
                """,
                (admin_id,),
            ).fetchone()
        )

    def log_action(
        self,
        conn: sqlite3.Connection,
        *,
        admin_user_id: int,
        target_user_id: int | None,
        action: str,
        payload: Dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO admin_action_logs (
                admin_user_id, target_user_id, action, payload, created_at
            )
            VALUES (?, ?, ?, ?::jsonb, ?)
            """,
            (admin_user_id, target_user_id, action, dumps(payload), iso_now()),
        )

    def stats(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        return dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM users) AS users_total,
                    (SELECT COUNT(*) FROM subscriptions
                        WHERE status = 'active' AND ends_at > ?) AS active_subscriptions,
                    (SELECT COUNT(*) FROM payments WHERE status = 'paid') AS paid_payments,
                    (SELECT COALESCE(SUM(amount_rub - discount_rub), 0)
                        FROM payments WHERE status = 'paid') AS mock_revenue_rub,
                    (SELECT COUNT(*) FROM generations) AS generations_total,
                    (SELECT COALESCE(SUM(coins_balance_cache), 0)
                        FROM subscriptions WHERE status = 'active' AND ends_at > ?)
                        AS active_balance_total
                """,
                (iso_now(), iso_now()),
            ).fetchone()
        )

    def list_users(
        self, conn: sqlite3.Connection, *, page: int, page_size: int
    ) -> List[Dict[str, Any]]:
        offset = max(page - 1, 0) * page_size
        return rows_to_dicts(
            conn.execute(
                """
                SELECT
                    u.*,
                    s.status AS subscription_status,
                    s.coins_balance_cache,
                    p.name AS plan_name
                FROM users u
                LEFT JOIN subscriptions s
                    ON s.id = (
                        SELECT s2.id
                        FROM subscriptions s2
                        WHERE s2.user_id = u.id
                        ORDER BY
                            CASE WHEN s2.status = 'active' THEN 0 ELSE 1 END,
                            s2.ends_at DESC
                        LIMIT 1
                    )
                LEFT JOIN plans p ON p.id = s.plan_id
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        )

    def count_users(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"] if row else 0)

    def find_user(self, conn: sqlite3.Connection, query: str) -> Dict[str, Any] | None:
        normalized = query.strip().lstrip("@")
        if not normalized:
            return None
        if normalized.isdigit():
            row = conn.execute(
                """
                SELECT * FROM users
                WHERE id = ? OR telegram_id = ?
                ORDER BY CASE WHEN telegram_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (int(normalized), int(normalized), int(normalized)),
            ).fetchone()
            return row_to_dict(row)

        return row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1",
                (normalized,),
            ).fetchone()
        )

    def user_card(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any] | None:
        user = row_to_dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        )
        if user is None:
            return None

        subscription = row_to_dict(
            conn.execute(
                """
                SELECT s.*, p.name AS plan_name, p.code AS plan_code
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.user_id = ?
                ORDER BY
                    CASE WHEN s.status = 'active' AND s.ends_at > ? THEN 0 ELSE 1 END,
                    s.ends_at DESC
                LIMIT 1
                """,
                (user_id, iso_now()),
            ).fetchone()
        )
        payments = dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END), 0)
                        AS paid_count,
                    COALESCE(SUM(CASE WHEN status = 'paid'
                        THEN amount_rub - discount_rub ELSE 0 END), 0)
                        AS paid_amount_rub
                FROM payments
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        )
        generations = dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN coins_charged ELSE 0 END), 0) AS spent_coins
                FROM generations
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        )
        user["subscription"] = subscription
        user["payments"] = payments
        user["generations"] = generations
        return user

    def set_blocked(
        self, conn: sqlite3.Connection, *, user_id: int, is_blocked: bool
    ) -> None:
        conn.execute(
            "UPDATE users SET is_blocked = ? WHERE id = ?",
            (bool(is_blocked), user_id),
        )
