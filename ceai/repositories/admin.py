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

    def dashboard_stats(
        self, conn: sqlite3.Connection, *, period_started_at: str
    ) -> Dict[str, Any]:
        now = iso_now()
        return dict(
            conn.execute(
                """
                WITH regular_users AS (
                    SELECT u.id
                    FROM users u
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM admin_users au
                        WHERE au.user_id = u.id
                          AND au.is_active = TRUE
                    )
                )
                SELECT
                    (SELECT COUNT(*) FROM regular_users) AS users_total,
                    (SELECT COUNT(*)
                        FROM users u
                        JOIN regular_users ru ON ru.id = u.id
                        WHERE u.created_at >= ?) AS users_period,
                    (SELECT COUNT(*)
                        FROM users u
                        JOIN regular_users ru ON ru.id = u.id
                        WHERE u.is_blocked = TRUE) AS blocked_users,
                    (SELECT COUNT(DISTINCT user_id)
                        FROM admin_users
                        WHERE is_active = TRUE) AS admin_users,
                    (SELECT COUNT(DISTINCT gift.user_id)
                        FROM coin_transactions gift
                        JOIN regular_users ru ON ru.id = gift.user_id
                        WHERE gift.reason = 'channel_gift'
                          AND gift.status = 'completed'
                          AND NOT EXISTS (
                              SELECT 1 FROM payments paid
                              WHERE paid.user_id = gift.user_id
                                AND paid.status = 'paid'
                                AND paid.provider <> 'mock'
                          )) AS trial_users,
                    (SELECT COUNT(DISTINCT paid.user_id)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider <> 'mock') AS paid_users,
                    (SELECT COUNT(*)
                        FROM subscriptions s
                        JOIN regular_users ru ON ru.id = s.user_id
                        WHERE s.status = 'active' AND s.ends_at > ?)
                        AS active_subscriptions,
                    (SELECT COUNT(DISTINCT s.user_id)
                        FROM subscriptions s
                        JOIN regular_users ru ON ru.id = s.user_id
                        WHERE s.status = 'active'
                          AND s.ends_at > ?
                          AND EXISTS (
                              SELECT 1 FROM payments p
                              WHERE p.user_id = s.user_id
                                AND p.status = 'paid'
                                AND p.provider <> 'mock'
                          )) AS active_paid_users,
                    (SELECT COUNT(DISTINCT s.user_id)
                        FROM subscriptions s
                        JOIN regular_users ru ON ru.id = s.user_id
                        WHERE s.status = 'active'
                          AND s.ends_at > ?
                          AND EXISTS (
                              SELECT 1 FROM coin_transactions ct
                              WHERE ct.user_id = s.user_id
                                AND ct.reason = 'channel_gift'
                                AND ct.status = 'completed'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM payments p
                              WHERE p.user_id = s.user_id
                                AND p.status = 'paid'
                                AND p.provider <> 'mock'
                          )) AS active_trial_users,
                    (SELECT COUNT(*)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider <> 'mock') AS paid_payments,
                    (SELECT COUNT(*)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider IN ('platega', 'yookassa'))
                        AS cash_paid_payments,
                    (SELECT COUNT(*)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider = 'platega') AS platega_paid_payments,
                    (SELECT COUNT(*)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider = 'telegram_stars')
                        AS stars_paid_payments,
                    (SELECT COUNT(*)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider = 'mock') AS mock_paid_payments,
                    (SELECT COALESCE(SUM(
                            paid.amount_rub - paid.discount_rub
                        ), 0)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider IN ('platega', 'yookassa'))
                        AS revenue_rub,
                    (SELECT COALESCE(SUM(
                            paid.amount_rub - paid.discount_rub
                        ), 0)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider = 'platega') AS platega_revenue_rub,
                    (SELECT COALESCE(SUM(
                            paid.amount_rub - paid.discount_rub
                        ), 0)
                        FROM payments paid
                        JOIN regular_users ru ON ru.id = paid.user_id
                        WHERE paid.status = 'paid'
                          AND paid.provider IN ('platega', 'yookassa')
                          AND paid.paid_at >= ?) AS revenue_period_rub,
                    (SELECT COUNT(*)
                        FROM generations g
                        JOIN regular_users ru ON ru.id = g.user_id)
                        AS generations_total,
                    (SELECT COUNT(*)
                        FROM generations g
                        JOIN regular_users ru ON ru.id = g.user_id
                        WHERE g.created_at >= ?) AS generations_period,
                    (SELECT COALESCE(SUM(s.coins_balance_cache), 0)
                        FROM subscriptions s
                        JOIN regular_users ru ON ru.id = s.user_id
                        WHERE s.status = 'active' AND s.ends_at > ?)
                        AS active_balance_total
                """,
                (
                    period_started_at,
                    now,
                    now,
                    now,
                    period_started_at,
                    period_started_at,
                    now,
                ),
            ).fetchone()
        )

    @staticmethod
    def _web_user_filter(segment: str) -> str:
        filters = {
            "all": "",
            "trial": """
                AND NOT EXISTS (
                    SELECT 1 FROM admin_users au_filter
                    WHERE au_filter.user_id = u.id
                      AND au_filter.is_active = TRUE
                )
                AND EXISTS (
                    SELECT 1 FROM coin_transactions ct_filter
                    WHERE ct_filter.user_id = u.id
                      AND ct_filter.reason = 'channel_gift'
                      AND ct_filter.status = 'completed'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM payments pay_filter
                    WHERE pay_filter.user_id = u.id
                      AND pay_filter.status = 'paid'
                      AND pay_filter.provider <> 'mock'
                )
            """,
            "paid": """
                AND NOT EXISTS (
                    SELECT 1 FROM admin_users au_filter
                    WHERE au_filter.user_id = u.id
                      AND au_filter.is_active = TRUE
                )
                AND EXISTS (
                    SELECT 1 FROM payments pay_filter
                    WHERE pay_filter.user_id = u.id
                      AND pay_filter.status = 'paid'
                      AND pay_filter.provider <> 'mock'
                )
            """,
            "active": """
                AND EXISTS (
                    SELECT 1 FROM subscriptions sub_filter
                    WHERE sub_filter.user_id = u.id
                      AND sub_filter.status = 'active'
                      AND sub_filter.ends_at > ?
                )
            """,
            "blocked": "AND u.is_blocked = TRUE",
        }
        return filters.get(segment, "")

    def count_web_users(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        segment: str,
    ) -> int:
        normalized = query.strip().lstrip("@").lower()
        params: list[Any] = []
        search_filter = ""
        if normalized:
            search_filter = """
                AND (
                    LOWER(COALESCE(u.username, '')) LIKE ?
                    OR LOWER(COALESCE(u.first_name, '')) LIKE ?
                    OR LOWER(COALESCE(u.last_name, '')) LIKE ?
                    OR CAST(u.telegram_id AS TEXT) LIKE ?
                    OR CAST(u.id AS TEXT) LIKE ?
                )
            """
            pattern = f"%{normalized}%"
            params.extend([pattern, pattern, pattern, pattern, pattern])
        segment_filter = self._web_user_filter(segment)
        if segment == "active":
            params.append(iso_now())
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM users u
            WHERE TRUE
            {search_filter}
            {segment_filter}
            """,
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_web_users(
        self,
        conn: sqlite3.Connection,
        *,
        page: int,
        page_size: int,
        query: str,
        segment: str,
    ) -> List[Dict[str, Any]]:
        normalized = query.strip().lstrip("@").lower()
        params: list[Any] = []
        search_filter = ""
        if normalized:
            search_filter = """
                AND (
                    LOWER(COALESCE(u.username, '')) LIKE ?
                    OR LOWER(COALESCE(u.first_name, '')) LIKE ?
                    OR LOWER(COALESCE(u.last_name, '')) LIKE ?
                    OR CAST(u.telegram_id AS TEXT) LIKE ?
                    OR CAST(u.id AS TEXT) LIKE ?
                )
            """
            pattern = f"%{normalized}%"
            params.extend([pattern, pattern, pattern, pattern, pattern])
        segment_filter = self._web_user_filter(segment)
        now = iso_now()
        if segment == "active":
            params.append(now)
        query_params = [
            now,
            now,
            *params,
            page_size,
            max(page - 1, 0) * page_size,
        ]
        return rows_to_dicts(
            conn.execute(
                f"""
                SELECT
                    u.*,
                    s.status AS subscription_status,
                    s.ends_at AS subscription_ends_at,
                    CASE
                        WHEN s.status = 'active' AND s.ends_at > ?
                        THEN TRUE ELSE FALSE
                    END AS subscription_is_active,
                    COALESCE(s.coins_balance_cache, 0) AS coins_balance_cache,
                    p.name AS plan_name,
                    EXISTS (
                        SELECT 1 FROM coin_transactions ct
                        WHERE ct.user_id = u.id
                          AND ct.reason = 'channel_gift'
                          AND ct.status = 'completed'
                    ) AS has_trial,
                    EXISTS (
                        SELECT 1 FROM payments pay
                        WHERE pay.user_id = u.id
                          AND pay.status = 'paid'
                          AND pay.provider <> 'mock'
                    ) AS has_paid,
                    EXISTS (
                        SELECT 1 FROM admin_users au
                        WHERE au.user_id = u.id
                          AND au.is_active = TRUE
                    ) AS is_admin,
                    (
                        SELECT COUNT(*) FROM generations g
                        WHERE g.user_id = u.id
                    ) AS generations_total
                FROM users u
                LEFT JOIN subscriptions s
                    ON s.id = (
                        SELECT s2.id
                        FROM subscriptions s2
                        WHERE s2.user_id = u.id
                        ORDER BY
                            CASE
                                WHEN s2.status = 'active' AND s2.ends_at > ?
                                THEN 0 ELSE 1
                            END,
                            s2.ends_at DESC
                        LIMIT 1
                    )
                LEFT JOIN plans p ON p.id = s.plan_id
                WHERE TRUE
                {search_filter}
                {segment_filter}
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(query_params),
            ).fetchall()
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
                    COALESCE(SUM(CASE
                        WHEN status = 'paid' AND provider <> 'mock'
                        THEN 1 ELSE 0 END), 0)
                        AS paid_count,
                    COALESCE(SUM(CASE
                        WHEN status = 'paid'
                          AND provider IN ('platega', 'yookassa')
                        THEN amount_rub - discount_rub ELSE 0 END), 0)
                        AS paid_amount_rub,
                    COALESCE(SUM(CASE
                        WHEN status = 'paid' AND provider = 'telegram_stars'
                        THEN 1 ELSE 0 END), 0) AS stars_paid_count,
                    COALESCE(SUM(CASE
                        WHEN status = 'paid' AND provider = 'mock'
                        THEN 1 ELSE 0 END), 0) AS mock_paid_count
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
        recent_payments = rows_to_dicts(
            conn.execute(
                """
                SELECT
                    pay.id, pay.provider, pay.status, pay.amount_rub,
                    pay.discount_rub, pay.created_at, pay.paid_at,
                    p.name AS plan_name
                FROM payments pay
                JOIN plans p ON p.id = pay.plan_id
                WHERE pay.user_id = ?
                ORDER BY pay.created_at DESC
                LIMIT 5
                """,
                (user_id,),
            ).fetchall()
        )
        recent_generations = rows_to_dicts(
            conn.execute(
                """
                SELECT
                    g.id, g.status, g.generation_type, g.coins_charged,
                    g.created_at, mp.display_name AS model_name
                FROM generations g
                JOIN model_prices mp ON mp.id = g.model_price_id
                WHERE g.user_id = ?
                ORDER BY g.created_at DESC
                LIMIT 5
                """,
                (user_id,),
            ).fetchall()
        )
        flags = dict(
            conn.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM coin_transactions ct
                        WHERE ct.user_id = ?
                          AND ct.reason = 'channel_gift'
                          AND ct.status = 'completed'
                    ) AS has_trial,
                    EXISTS (
                        SELECT 1 FROM payments pay
                        WHERE pay.user_id = ?
                          AND pay.status = 'paid'
                          AND pay.provider <> 'mock'
                    ) AS has_paid,
                    EXISTS (
                        SELECT 1 FROM admin_users au
                        WHERE au.user_id = ?
                          AND au.is_active = TRUE
                    ) AS is_admin,
                    (
                        SELECT COUNT(*) FROM users invited
                        WHERE invited.referred_by_user_id = ?
                    ) AS invited_count
                """,
                (user_id, user_id, user_id, user_id),
            ).fetchone()
        )
        user["subscription"] = subscription
        user["payments"] = payments
        user["generations"] = generations
        user["recent_payments"] = recent_payments
        user["recent_generations"] = recent_generations
        user.update(flags)
        return user

    def set_blocked(
        self, conn: sqlite3.Connection, *, user_id: int, is_blocked: bool
    ) -> None:
        conn.execute(
            "UPDATE users SET is_blocked = ? WHERE id = ?",
            (bool(is_blocked), user_id),
        )
