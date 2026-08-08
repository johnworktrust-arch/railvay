from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any, Dict, List

from ceavpn.repositories.base import row_to_dict, rows_to_dicts
from ceavpn.time_utils import iso_now


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
                    COALESCE(p.max_devices, 2) + COALESCE(s.extra_devices, 0) AS plan_max_devices
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
                    COALESCE(p.max_devices, 2) + COALESCE(s.extra_devices, 0) AS plan_max_devices
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
                    COALESCE(p.max_devices, 2) + COALESCE(s.extra_devices, 0) AS plan_max_devices
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

    def list_for_user(
        self,
        conn: sqlite3.Connection,
        user_id: int,
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_subscriptions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
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

    def ensure_provider_uuid(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
    ) -> str:
        candidate = str(uuid.uuid4())
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET provider_uuid = CASE
                    WHEN provider_uuid IS NULL OR provider_uuid = '' THEN ?
                    ELSE provider_uuid
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (candidate, iso_now(), subscription_id),
        )
        subscription = self._require_by_id(
            conn, subscription_id, "assign provider UUID to"
        )
        value = str(subscription.get("provider_uuid") or "")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise RuntimeError("VPN subscription has an invalid provider UUID") from exc
        if parsed.version != 4:
            raise RuntimeError("VPN subscription provider UUID is not UUIDv4")
        return str(parsed)

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

    def list_active_requiring_profile_update(
        self,
        conn: sqlite3.Connection,
        *,
        server_id: int,
        profile_version: str,
        worker_epoch: str,
        active_at: str | None = None,
        limit: int = 1,
    ) -> List[Dict[str, Any]]:
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", profile_version):
            raise ValueError("invalid VPN profile version")
        if (
            worker_epoch != "legacy"
            and re.fullmatch(r"e[0-9a-f]{32}", worker_epoch) is None
        ):
            raise ValueError("invalid VPN worker epoch")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        idempotency_prefix = (
            f"vpn:profile:{profile_version}:epoch:{worker_epoch}:"
        )
        return rows_to_dicts(
            conn.execute(
                """
                SELECT s.*
                FROM vpn_subscriptions s
                WHERE s.server_id = ?
                  AND s.status = 'active'
                  AND s.ends_at > ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM vpn_provisioning_jobs job
                      WHERE job.subscription_id = s.id
                        AND job.idempotency_key =
                            ? || CAST(s.id AS TEXT)
                  )
                ORDER BY s.id ASC
                LIMIT ?
                """,
                (
                    server_id,
                    active_at or iso_now(),
                    idempotency_prefix,
                    limit,
                ),
            ).fetchall()
        )

    def list_active_requiring_server_replica(
        self,
        conn: sqlite3.Connection,
        *,
        server_id: int,
        profile_version: str,
        worker_epoch: str,
        active_at: str | None = None,
        limit: int = 1,
    ) -> List[Dict[str, Any]]:
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", profile_version):
            raise ValueError("invalid VPN profile version")
        if (
            worker_epoch != "legacy"
            and re.fullmatch(r"e[0-9a-f]{32}", worker_epoch) is None
        ):
            raise ValueError("invalid VPN worker epoch")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        replica_key = (
            f"vpn:replica:{profile_version}:epoch:{worker_epoch}:"
        )
        return rows_to_dicts(
            conn.execute(
                """
                SELECT s.*
                FROM vpn_subscriptions s
                WHERE s.server_id <> ?
                  AND s.status = 'active'
                  AND s.ends_at > ?
                  AND s.provider_uuid IS NOT NULL
                  AND s.provider_uuid <> ''
                  AND s.subscription_url IS NOT NULL
                  AND s.subscription_url <> ''
                  AND EXISTS (
                      SELECT 1
                      FROM vpn_provisioning_jobs canonical_job
                      WHERE canonical_job.subscription_id = s.id
                        AND canonical_job.server_id = s.server_id
                        AND canonical_job.status = 'completed'
                        AND canonical_job.operation IN ('create', 'update')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM vpn_provisioning_jobs replica_job
                      WHERE replica_job.subscription_id = s.id
                        AND replica_job.server_id = ?
                        AND replica_job.idempotency_key =
                            ? || CAST(s.id AS TEXT) ||
                            ':server:' || CAST(? AS TEXT)
                  )
                ORDER BY s.id ASC
                LIMIT ?
                """,
                (
                    server_id,
                    active_at or iso_now(),
                    server_id,
                    replica_key,
                    server_id,
                    limit,
                ),
            ).fetchall()
        )

    def has_completed_server_replica(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        server_code: str,
        profile_version: str,
        healthy_after: str,
        active_at: str,
    ) -> bool:
        if subscription_id <= 0:
            return False
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", server_code) is None:
            return False
        if re.fullmatch(r"p[0-9a-f]{20}", profile_version) is None:
            return False
        replica_prefix = f"vpn:replica:{profile_version}:epoch:"
        row = conn.execute(
            """
            SELECT 1 AS ready
            FROM vpn_servers server
            JOIN vpn_provisioning_jobs job
              ON job.server_id = server.id
             AND job.subscription_id = ?
            JOIN vpn_subscriptions subscription
              ON subscription.id = job.subscription_id
            WHERE server.code = ?
              AND server.is_active = TRUE
              AND server.worker_id IS NOT NULL
              AND server.worker_id <> ''
              AND server.last_health_at IS NOT NULL
              AND server.last_health_at >= ?
              AND server.current_profile_version = ?
              AND server.current_worker_epoch IS NOT NULL
              AND server.current_worker_epoch <> 'legacy'
              AND LENGTH(server.current_worker_epoch) = 33
              AND server.current_worker_epoch LIKE 'e%'
              AND subscription.server_id <> server.id
              AND subscription.status = 'active'
              AND subscription.ends_at > ?
              AND subscription.provider_uuid IS NOT NULL
              AND subscription.provider_uuid <> ''
              AND job.operation = 'update'
              AND job.status = 'completed'
              AND job.completed_at IS NOT NULL
              AND job.idempotency_key =
                  ? || server.current_worker_epoch || ':' ||
                  CAST(subscription.id AS TEXT) || ':server:' ||
                  CAST(server.id AS TEXT)
              AND (
                  SELECT latest.status
                  FROM vpn_provisioning_jobs latest
                  WHERE latest.subscription_id = subscription.id
                    AND latest.server_id = server.id
                    AND latest.operation IN ('create', 'update')
                  ORDER BY latest.id DESC
                  LIMIT 1
              ) = 'completed'
            LIMIT 1
            """,
            (
                subscription_id,
                server_code,
                healthy_after,
                profile_version,
                active_at,
                replica_prefix,
            ),
        ).fetchone()
        return row is not None

    def add_extra_devices(
        self,
        conn: Any,
        *,
        subscription_id: int,
        count: int,
    ) -> None:
        conn.execute(
            """
            UPDATE vpn_subscriptions
            SET extra_devices = extra_devices + ?
            WHERE id = ?
            """,
            (count, subscription_id),
        )

    def _require_by_id(
        self, conn: sqlite3.Connection, subscription_id: int, action: str
    ) -> Dict[str, Any]:
        subscription = self.get_by_id(conn, subscription_id)
        if subscription is None:
            raise RuntimeError(f"Could not {action} VPN subscription")
        return subscription
