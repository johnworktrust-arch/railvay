from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class VpnSubscriptionRepository:
    def create_provisioning(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        server_id: int,
        plan_id: int | None,
        kind: str,
        provider_username: str,
        starts_at: str,
        ends_at: str,
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO vpn_subscriptions (
                user_id, server_id, plan_id, kind, billing_kind, status,
                provider_username, subscription_url, starts_at, ends_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'provisioning', ?, '', ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id,
                server_id,
                plan_id,
                kind,
                "paid" if plan_id is not None else kind,
                provider_username,
                starts_at,
                ends_at,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        subscription = self.get_by_id(conn, int(row["id"]))
        if subscription is None:
            raise RuntimeError("Could not create VPN subscription")
        return subscription

    def get_by_id(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT
                    s.*,
                    srv.code AS server_code,
                    srv.name AS server_name,
                    srv.provider AS server_provider,
                    srv.region AS server_region,
                    p.code AS plan_code,
                    p.name AS plan_name,
                    p.duration_days AS plan_duration_days,
                    p.max_devices AS plan_max_devices
                FROM vpn_subscriptions s
                JOIN vpn_servers srv ON srv.id = s.server_id
                LEFT JOIN vpn_plans p ON p.id = s.plan_id
                WHERE s.id = ?
                """,
                (subscription_id,),
            ).fetchone()
        )

    def expire_stale_for_user(
        self, conn: sqlite3.Connection, *, user_id: int, now: str | None = None
    ) -> None:
        current = now or iso_now()
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET status = 'expired', updated_at = ?
            WHERE user_id = ?
              AND status = 'active'
              AND ends_at <= ?
            """,
            (current, user_id, current),
        )

    def get_active_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        self.expire_stale_for_user(conn, user_id=user_id)
        return row_to_dict(
            conn.execute(
                """
                SELECT
                    s.*,
                    srv.code AS server_code,
                    srv.name AS server_name,
                    srv.provider AS server_provider,
                    srv.region AS server_region,
                    p.code AS plan_code,
                    p.name AS plan_name,
                    p.duration_days AS plan_duration_days,
                    p.max_devices AS plan_max_devices
                FROM vpn_subscriptions s
                JOIN vpn_servers srv ON srv.id = s.server_id
                LEFT JOIN vpn_plans p ON p.id = s.plan_id
                WHERE s.user_id = ?
                  AND s.status = 'active'
                  AND s.ends_at > ?
                ORDER BY s.ends_at DESC
                LIMIT 1
                """,
                (user_id, iso_now()),
            ).fetchone()
        )

    def get_live_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        self.expire_stale_for_user(conn, user_id=user_id)
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_subscriptions
                WHERE user_id = ?
                  AND status IN ('provisioning', 'active')
                ORDER BY ends_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        )

    def get_latest_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        self.expire_stale_for_user(conn, user_id=user_id)
        return row_to_dict(
            conn.execute(
                """
                SELECT
                    s.*,
                    srv.code AS server_code,
                    srv.name AS server_name,
                    srv.provider AS server_provider,
                    srv.region AS server_region,
                    p.code AS plan_code,
                    p.name AS plan_name,
                    p.duration_days AS plan_duration_days,
                    p.max_devices AS plan_max_devices
                FROM vpn_subscriptions s
                JOIN vpn_servers srv ON srv.id = s.server_id
                LEFT JOIN vpn_plans p ON p.id = s.plan_id
                WHERE s.user_id = ?
                ORDER BY
                    CASE s.status
                        WHEN 'active' THEN 0
                        WHEN 'provisioning' THEN 1
                        WHEN 'error' THEN 2
                        WHEN 'expired' THEN 3
                        ELSE 4
                    END,
                    s.created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        )

    def mark_active(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        subscription_url: str,
        synced_at: str | None = None,
    ) -> Dict[str, Any]:
        now = synced_at or iso_now()
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET status = 'active',
                subscription_url = ?,
                last_synced_at = ?,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (subscription_url, now, now, subscription_id),
        )
        return self._require_by_id(conn, subscription_id, "activate")

    def mark_status(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        status: str,
        last_error: str | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET status = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, last_error, now, subscription_id),
        )
        return self._require_by_id(conn, subscription_id, "update")

    def update_period(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        plan_id: int | None,
        kind: str,
        starts_at: str,
        ends_at: str,
        status: str = "provisioning",
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET plan_id = ?, kind = ?, billing_kind = ?, status = ?,
                starts_at = ?, ends_at = ?,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                plan_id,
                kind,
                "paid" if plan_id is not None else kind,
                status,
                starts_at,
                ends_at,
                iso_now(),
                subscription_id,
            ),
        )
        return self._require_by_id(conn, subscription_id, "update period for")

    def list_due_for_expiration(
        self,
        conn: sqlite3.Connection,
        *,
        due_at: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_subscriptions
                WHERE status IN ('active', 'expired') AND ends_at <= ?
                ORDER BY ends_at ASC
                LIMIT ?
                """,
                (due_at or iso_now(), limit),
            ).fetchall()
        )

    def _require_by_id(
        self, conn: sqlite3.Connection, subscription_id: int, action: str
    ) -> Dict[str, Any]:
        subscription = self.get_by_id(conn, subscription_id)
        if subscription is None:
            raise RuntimeError(f"Could not {action} VPN subscription")
        return subscription
