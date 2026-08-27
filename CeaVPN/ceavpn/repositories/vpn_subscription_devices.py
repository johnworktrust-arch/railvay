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
        legacy_user_agent_suffix: str = "",
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
            model, platform = self._preferred_metadata(existing, model, platform)
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

        aliases = []
        normalized_suffix = legacy_user_agent_suffix.strip().lower()
        if normalized_suffix:
            aliases = [
                item
                for item in self._list_all_active(
                    conn, subscription_id=subscription_id
                )
                if str(item.get("user_agent") or "")
                .strip()
                .lower()
                .endswith(normalized_suffix)
            ]
        if aliases:
            aliases.sort(
                key=lambda item: (
                    self._model_quality(str(item.get("model") or "")),
                    str(item.get("last_seen_at") or ""),
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
            canonical = existing or aliases[0]
            duplicate_ids = {
                int(item["id"])
                for item in aliases
                if int(item["id"]) != int(canonical["id"])
            }
            if duplicate_ids:
                placeholders = ", ".join("?" for _ in duplicate_ids)
                conn.execute(
                    f"""
                    UPDATE vpn_subscription_devices
                    SET deactivated_at = ?, updated_at = ?
                    WHERE subscription_id = ? AND id IN ({placeholders})
                      AND deactivated_at IS NULL
                    """,
                    (now, now, subscription_id, *sorted(duplicate_ids)),
                )
            model, platform = self._preferred_metadata(canonical, model, platform)
            conn.execute(
                """
                UPDATE vpn_subscription_devices
                SET device_key = ?, model = ?, platform = ?, user_agent = ?,
                    last_seen_at = ?, deactivated_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    device_key,
                    model,
                    platform,
                    user_agent,
                    now,
                    now,
                    int(canonical["id"]),
                ),
            )
            return self.get_by_id(conn, int(canonical["id"])) or canonical

        active_count = self.active_count(conn, subscription_id=subscription_id)
        if active_count >= max_devices:
            raise DeviceLimitExceededError()

        if existing is not None:
            model, platform = self._preferred_metadata(existing, model, platform)
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

    @staticmethod
    def _model_quality(value: str) -> int:
        normalized = value.strip().lower()
        if normalized in {
            "",
            "не определено",
            "устройство cea vpn",
            "android-устройство",
            "iphone",
            "ipad",
            "компьютер",
            "mac",
        }:
            return 0
        return 1

    @classmethod
    def _preferred_metadata(
        cls,
        existing: Dict[str, Any],
        model: str,
        platform: str,
    ) -> tuple[str, str]:
        current_model = str(existing.get("model") or "").strip()
        if cls._model_quality(model) < cls._model_quality(current_model):
            model = current_model
        current_platform = str(existing.get("platform") or "").strip()
        if platform.strip().lower() in {
            "",
            "не определено",
            "платформа не передана клиентом",
        } and current_platform:
            platform = current_platform
        return model, platform

    def _list_all_active(
        self, conn: Any, *, subscription_id: int
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_subscription_devices
                WHERE subscription_id = ? AND deactivated_at IS NULL
                ORDER BY id ASC
                """,
                (subscription_id,),
            ).fetchall()
        )

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
