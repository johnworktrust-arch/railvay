from __future__ import annotations

from typing import Any, Dict, List

from ceavpn.repositories.base import row_to_dict, rows_to_dicts
from ceavpn.time_utils import iso_now


class DeviceLimitExceededError(Exception):
    """Raised when a new client cannot occupy a subscription device slot."""


class VpnSubscriptionDeviceRepository:
    def register_or_touch(
        self,
        conn: Any,
        *,
        subscription_id: int,
        device_key: str,
        model: str,
        platform: str,
        user_agent: str,
        max_devices: int,
    ) -> Dict[str, Any]:
        """Atomically reserve a slot for a device or update an existing one."""

        if max_devices <= 0:
            raise DeviceLimitExceededError()
        if getattr(conn, "driver", "") == "postgres":
            # All registrations for one subscription serialize on this row.
            conn.execute(
                "SELECT id FROM vpn_subscriptions WHERE id = ? FOR UPDATE",
                (subscription_id,),
            ).fetchone()

        now = iso_now()
        existing = self.get_by_key(
            conn, subscription_id=subscription_id, device_key=device_key
        )
        if existing is not None and existing.get("deactivated_at") is None:
            conn.execute(
                """
                UPDATE vpn_subscription_devices
                SET model = ?, platform = ?, user_agent = ?, last_seen_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (model, platform, user_agent, now, now, int(existing["id"])),
            )
            return self.get_by_id(conn, int(existing["id"])) or existing

        active_count = self.active_count(conn, subscription_id=subscription_id)
        if active_count >= max_devices:
            raise DeviceLimitExceededError()

        if existing is not None:
            conn.execute(
                """
                UPDATE vpn_subscription_devices
                SET model = ?, platform = ?, user_agent = ?, first_seen_at = ?,
                    last_seen_at = ?, deactivated_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (model, platform, user_agent, now, now, now, int(existing["id"])),
            )
            return self.get_by_id(conn, int(existing["id"])) or existing

        cursor = conn.execute(
            """
            INSERT INTO vpn_subscription_devices (
                subscription_id, device_key, model, platform, user_agent,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (subscription_id, device_key, model, platform, user_agent, now, now, now, now),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Could not register VPN device")
        device = self.get_by_id(conn, int(row["id"]))
        if device is None:
            raise RuntimeError("Could not load registered VPN device")
        return device

    def list_active(
        self, conn: Any, *, subscription_id: int, offset: int, limit: int
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_subscription_devices
                WHERE subscription_id = ? AND deactivated_at IS NULL
                ORDER BY first_seen_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (subscription_id, limit, offset),
            ).fetchall()
        )

    def active_count(self, conn: Any, *, subscription_id: int) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM vpn_subscription_devices
            WHERE subscription_id = ? AND deactivated_at IS NULL
            """,
            (subscription_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def get_by_id(self, conn: Any, device_id: int) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_subscription_devices WHERE id = ?", (device_id,)
            ).fetchone()
        )

    def get_by_key(
        self, conn: Any, *, subscription_id: int, device_key: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_subscription_devices
                WHERE subscription_id = ? AND device_key = ?
                """,
                (subscription_id, device_key),
            ).fetchone()
        )

    def deactivate(
        self, conn: Any, *, subscription_id: int, device_id: int
    ) -> bool:
        now = iso_now()
        cursor = conn.execute(
            """
            UPDATE vpn_subscription_devices
            SET deactivated_at = ?, updated_at = ?
            WHERE id = ? AND subscription_id = ? AND deactivated_at IS NULL
            RETURNING id
            """,
            (now, now, device_id, subscription_id),
        )
        return cursor.fetchone() is not None
