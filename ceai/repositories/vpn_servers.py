from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


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
        is_active: bool = True,
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
                last_health_at = CASE
                    WHEN COALESCE(vpn_servers.worker_id, '') <>
                         COALESCE(excluded.worker_id, '')
                      OR COALESCE(vpn_servers.subscription_base_url, '') <>
                         COALESCE(excluded.subscription_base_url, '')
                    THEN NULL
                    ELSE vpn_servers.last_health_at
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
                bool(is_active),
                worker_id.strip() or None,
                subscription_base_url.strip().rstrip("/"),
                now,
                now,
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
        """Return a server only when its outbound worker polled recently."""

        return row_to_dict(
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
    ) -> Dict[str, Any]:
        now = checked_at or iso_now()
        conn.execute(
            """
            UPDATE vpn_servers
            SET last_health_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, server_id),
        )
        server = self.get_by_id(conn, server_id)
        if server is None:
            raise RuntimeError("Could not mark VPN server healthy")
        return server
