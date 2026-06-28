from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now, parse_iso, utcnow


class SubscriptionRepository:
    def expire_stale_for_user(self, conn: sqlite3.Connection, user_id: int) -> None:
        now = iso_now()
        conn.execute(
            """
            UPDATE subscriptions
            SET status = 'expired', updated_at = ?
            WHERE user_id = ? AND status = 'active' AND ends_at <= ?
            """,
            (now, user_id, now),
        )

    def get_active_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        self.expire_stale_for_user(conn, user_id)
        return row_to_dict(
            conn.execute(
                """
                SELECT s.*, p.name AS plan_name, p.code AS plan_code
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.user_id = ? AND s.status = 'active' AND s.ends_at > ?
                ORDER BY s.ends_at DESC
                LIMIT 1
                """,
                (user_id, iso_now()),
            ).fetchone()
        )

    def extend_or_create_active(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        duration_days: int,
    ) -> Dict[str, Any]:
        self.expire_stale_for_user(conn, user_id)
        now_dt = utcnow()
        now = now_dt.isoformat()
        active = self.get_active_for_user(conn, user_id)
        if active:
            current_end = parse_iso(active["ends_at"])
            base = current_end if current_end > now_dt else now_dt
            new_end = (base + timedelta(days=duration_days)).isoformat()
            conn.execute(
                """
                UPDATE subscriptions
                SET plan_id = ?, ends_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (plan_id, new_end, now, active["id"]),
            )
            updated = self.get_by_id(conn, active["id"])
            if updated is None:
                raise RuntimeError("Could not extend subscription")
            return updated

        starts_at = now
        ends_at = (now_dt + timedelta(days=duration_days)).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO subscriptions (
                user_id, plan_id, status, coins_balance_cache, auto_renew,
                starts_at, ends_at, created_at, updated_at
            )
            VALUES (?, ?, 'active', 0, FALSE, ?, ?, ?, ?)
            RETURNING id
            """,
            (user_id, plan_id, starts_at, ends_at, now, now),
        )
        row = cursor.fetchone()
        created = self.get_by_id(conn, int(row["id"]))
        if created is None:
            raise RuntimeError("Could not create subscription")
        return created

    def restore_paid_period(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        starts_at: datetime,
        duration_days: int,
        preferred_subscription_id: int | None = None,
    ) -> Dict[str, Any] | None:
        self.expire_stale_for_user(conn, user_id)
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)
        ends_at = starts_at + timedelta(days=duration_days)
        if ends_at <= utcnow():
            return None

        now = iso_now()
        starts = starts_at.isoformat()
        ends = ends_at.isoformat()
        if preferred_subscription_id:
            existing = self.get_by_id(conn, preferred_subscription_id)
            if existing and int(existing["user_id"]) == user_id:
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET plan_id = ?, status = 'active', starts_at = ?,
                        ends_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (plan_id, starts, ends, now, preferred_subscription_id),
                )
                return self.get_by_id(conn, preferred_subscription_id)

        active = self.get_active_for_user(conn, user_id)
        if active:
            return active

        cursor = conn.execute(
            """
            INSERT INTO subscriptions (
                user_id, plan_id, status, coins_balance_cache, auto_renew,
                starts_at, ends_at, created_at, updated_at
            )
            VALUES (?, ?, 'active', 0, FALSE, ?, ?, ?, ?)
            RETURNING id
            """,
            (user_id, plan_id, starts, ends, now, now),
        )
        row = cursor.fetchone()
        created = self.get_by_id(conn, int(row["id"]))
        if created is None:
            raise RuntimeError("Could not restore subscription")
        return created

    def get_by_id(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT s.*, p.name AS plan_name, p.code AS plan_code
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.id = ?
                """,
                (subscription_id,),
            ).fetchone()
        )

    def set_balance_cache(
        self, conn: sqlite3.Connection, *, subscription_id: int, balance: int
    ) -> None:
        conn.execute(
            """
            UPDATE subscriptions
            SET coins_balance_cache = ?, updated_at = ?
            WHERE id = ?
            """,
            (balance, iso_now(), subscription_id),
        )

    def configure_auto_renew(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        payment_method_id: str,
        is_active: bool,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE subscriptions
            SET auto_renew = ?,
                yookassa_payment_method_id = ?,
                auto_renew_last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (bool(is_active), payment_method_id, now, subscription_id),
        )
        updated = self.get_by_id(conn, subscription_id)
        if updated is None:
            raise RuntimeError("Could not configure auto renewal")
        return updated

    def disable_auto_renew_for_user(
        self, conn: sqlite3.Connection, *, user_id: int
    ) -> Dict[str, Any] | None:
        subscription = self.get_active_for_user(conn, user_id)
        if subscription is None:
            return None
        conn.execute(
            """
            UPDATE subscriptions
            SET auto_renew = FALSE,
                auto_renew_last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (iso_now(), subscription["id"]),
        )
        return self.get_by_id(conn, subscription["id"])

    def mark_auto_renew_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        error_message: str | None,
    ) -> None:
        conn.execute(
            """
            UPDATE subscriptions
            SET auto_renew_last_attempt_at = ?,
                auto_renew_last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (iso_now(), error_message, iso_now(), subscription_id),
        )

    def list_due_auto_renewals(
        self,
        conn: sqlite3.Connection,
        *,
        due_at: str,
        retry_before: str,
        limit: int,
    ) -> list[Dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT
                s.*,
                p.name AS plan_name,
                p.code AS plan_code,
                p.price_rub AS plan_price_rub,
                p.duration_days AS plan_duration_days,
                p.coins_amount AS plan_coins_amount
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.status = 'active'
                AND s.auto_renew = TRUE
                AND s.yookassa_payment_method_id IS NOT NULL
                AND s.ends_at <= ?
                AND (
                    s.auto_renew_last_attempt_at IS NULL
                    OR s.auto_renew_last_attempt_at <= ?
                )
            ORDER BY s.ends_at ASC
            LIMIT ?
            """,
            (due_at, retry_before, limit),
        ).fetchall()
        return rows_to_dicts(rows)
