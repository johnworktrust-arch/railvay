from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceaadmin.repositories.base import row_to_dict, rows_to_dicts
from ceaadmin.time_utils import iso_now


class VpnPromocodeRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        reward_type: str,
        reward_value: int,
        target_user_id: int | None = None,
        max_uses: int | None = None,
        starts_at: str | None = None,
        expires_at: str | None = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = iso_now()
        code_clean = code.strip().upper()
        cursor = conn.execute(
            """
            INSERT INTO vpn_promocodes (
                code, reward_type, reward_value, target_user_id, max_uses,
                used_count, starts_at, expires_at, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                code_clean,
                reward_type,
                reward_value,
                target_user_id,
                max_uses,
                starts_at,
                expires_at,
                1 if is_active else 0,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        promocode = self.get_by_id(conn, int(row["id"]))
        if promocode is None:
            raise RuntimeError("Could not create VPN promocode")
        return promocode

    def get_by_id(
        self, conn: sqlite3.Connection, promocode_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_promocodes WHERE id = ?",
                (promocode_id,),
            ).fetchone()
        )

    def list_all(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM vpn_promocodes ORDER BY id DESC"
            ).fetchall()
        )

    def toggle_active(
        self, conn: sqlite3.Connection, *, promocode_id: int, is_active: bool
    ) -> Dict[str, Any] | None:
        now = iso_now()
        conn.execute(
            """
            UPDATE vpn_promocodes
            SET is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if is_active else 0, now, promocode_id),
        )
        return self.get_by_id(conn, promocode_id)

    def delete_promocode(self, conn: sqlite3.Connection, promocode_id: int) -> bool:
        cursor = conn.execute(
            "DELETE FROM vpn_promocodes WHERE id = ?", (promocode_id,)
        )
        return cursor.rowcount > 0
