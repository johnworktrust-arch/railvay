from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceaadmin.repositories.base import row_to_dict
from ceaadmin.time_utils import iso_now


class VpnAbuseRepository:
    def get_for_user(
        self,
        conn: sqlite3.Connection,
        user_id: int,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_user_bans
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        )

    def is_blocked(
        self,
        conn: sqlite3.Connection,
        user_id: int,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1 AS blocked
            FROM vpn_user_bans
            WHERE user_id = ?
              AND is_active = TRUE
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return row is not None

    def set_blocked(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        is_blocked: bool,
        reason: str = "",
        admin_user_id: int | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        if is_blocked:
            conn.execute(
                """
                INSERT INTO vpn_user_bans (
                    user_id, is_active, reason, admin_user_id,
                    created_at, updated_at, lifted_at
                )
                VALUES (?, TRUE, ?, ?, ?, ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_active = TRUE,
                    reason = excluded.reason,
                    admin_user_id = excluded.admin_user_id,
                    updated_at = excluded.updated_at,
                    lifted_at = NULL
                """,
                (
                    user_id,
                    reason.strip(),
                    admin_user_id,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE vpn_user_bans
                SET is_active = FALSE,
                    updated_at = ?,
                    lifted_at = ?
                WHERE user_id = ?
                """,
                (now, now, user_id),
            )
        ban = self.get_for_user(conn, user_id)
        if ban is None:
            return {
                "user_id": user_id,
                "is_active": False,
                "reason": "",
                "admin_user_id": admin_user_id,
                "created_at": now,
                "updated_at": now,
                "lifted_at": now,
            }
        return ban
