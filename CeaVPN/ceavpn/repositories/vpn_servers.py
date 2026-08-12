from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from ceavpn.repositories.base import row_to_dict, rows_to_dicts
from ceavpn.time_utils import iso_now


class VpnServerRepository:
    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        name: str,
        provider: str,
        region: str,
        api_base_url: str,
        is_active: Optional[bool] = None,
        worker_id: str = "",
        subscription_base_url: str = "",
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO vpn_servers (
                code, name, provider, region, api_base_url, is_active,
                worker_id, subscription_base_url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                provider = excluded.provider,
                region = excluded.region,
                api_base_url = excluded.api_base_url,
                is_active = CASE
                    WHEN ? THEN excluded.is_active
                    ELSE vpn_servers.is_active
                END,
                last_health_at = CASE
                    WHEN COALESCE(vpn_servers.worker_id, '') <>
                         COALESCE(excluded.worker_id, '')
                      OR COALESCE(vpn_servers.subscription_base_url, '') <>
                         COALESCE(excluded.subscription_base_url, '')
                    THEN NULL
                    ELSE vpn_servers.last_health_at
                END,
                current_profile_version = CASE
                    WHEN COALESCE(vpn_servers.worker_id, '') <>
                         COALESCE(excluded.worker_id, '')
                      OR COALESCE(vpn_servers.subscription_base_url, '') <>
                         COALESCE(excluded.subscription_base_url, '')
                    THEN NULL
                    ELSE vpn_servers.current_profile_version
                END,
                current_worker_epoch = CASE
                    WHEN COALESCE(vpn_servers.worker_id, '') <>
                         COALESCE(excluded.worker_id, '')
                      OR COALESCE(vpn_servers.subscription_base_url, '') <>
                         COALESCE(excluded.subscription_base_url, '')
                    THEN NULL
                    ELSE vpn_servers.current_worker_epoch
                END,
                worker_id = excluded.worker_id,
                subscription_base_url = excluded.subscription_base_url,
                updated_at = excluded.updated_at
            """,
            (
                code,
                name,
                provider,
                region,
                api_base_url.rstrip("/"),
                True if is_active is None else bool(is_active),
                worker_id.strip() or None,
                subscription_base_url.strip().rstrip("/"),
                now,
                now,
                is_active is not None,
            ),
        )
        server = self.get_by_code(conn, code)
        if server is None:
            raise RuntimeError("Could not upsert VPN server")
        return server

    def get_by_worker_id(
        self, conn: sqlite3.Connection, worker_id: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_servers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
        )

    def get_by_id(
        self, conn: sqlite3.Connection, server_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_servers WHERE id = ?", (server_id,)
            ).fetchone()
        )

    def get_by_code(
        self, conn: sqlite3.Connection, code: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_servers WHERE code = ?", (code,)
            ).fetchone()
        )

    def get_checkout_ready_by_code(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        healthy_after: str,
    ) -> Dict[str, Any] | None:
        """Return a server for checkout, prioritizing recently polled servers."""

        server = row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_servers
                WHERE code = ?
                  AND is_active = TRUE
                  AND worker_id IS NOT NULL
                  AND worker_id <> ''
                  AND subscription_base_url <> ''
                  AND last_health_at IS NOT NULL
                  AND last_health_at >= ?
                """,
                (code, healthy_after),
            ).fetchone()
        )
        if server is not None:
            return server
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_servers
                WHERE code = ?
                  AND is_active = TRUE
                """,
                (code,),
            ).fetchone()
        )

    def list_active(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_servers
                WHERE is_active = TRUE
                ORDER BY code ASC
                """
            ).fetchall()
        )

    def set_active(
        self, conn: sqlite3.Connection, *, server_id: int, is_active: bool
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE vpn_servers
            SET is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (bool(is_active), iso_now(), server_id),
        )
        server = self.get_by_id(conn, server_id)
        if server is None:
            raise RuntimeError("Could not update VPN server")
        return server

    def mark_healthy(
        self,
        conn: sqlite3.Connection,
        *,
        server_id: int,
        checked_at: str | None = None,
        profile_version: str | None = None,
        worker_epoch: str | None = None,
    ) -> Dict[str, Any]:
        if (profile_version is None) != (worker_epoch is None):
            raise ValueError(
                "VPN server profile version and worker epoch must be paired"
            )
        if (
            profile_version is not None
            and re.fullmatch(r"p[0-9a-f]{20}", profile_version) is None
        ):
            raise ValueError("invalid VPN server profile version")
        if (
            worker_epoch is not None
            and worker_epoch != "legacy"
            and re.fullmatch(r"e[0-9a-f]{32}", worker_epoch) is None
        ):
            raise ValueError("invalid VPN worker epoch")
        now = checked_at or iso_now()
        conn.execute(
            """
            UPDATE vpn_servers
            SET last_health_at = ?,
                current_profile_version = CASE
                    WHEN ? THEN ?
                    ELSE current_profile_version
                END,
                current_worker_epoch = CASE
                    WHEN ? THEN ?
                    ELSE current_worker_epoch
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                now,
                profile_version is not None,
                profile_version,
                worker_epoch is not None,
                worker_epoch,
                now,
                server_id,
            ),
        )
        server = self.get_by_id(conn, server_id)
        if server is None:
            raise RuntimeError("Could not mark VPN server healthy")
        return server
