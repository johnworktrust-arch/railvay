from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class UserRepository:
    def upsert_telegram_user(
        self,
        conn: sqlite3.Connection,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        referral_code = f"tg{telegram_id}"
        conn.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, last_name, language_code,
                referral_code, created_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                last_seen_at = excluded.last_seen_at
            """,
            (
                telegram_id,
                username,
                first_name,
                last_name,
                language_code,
                referral_code,
                now,
                now,
            ),
        )
        user = self.get_by_telegram_id(conn, telegram_id)
        if user is None:
            raise RuntimeError("Could not upsert Telegram user")
        return user

    def get_by_telegram_id(
        self, conn: sqlite3.Connection, telegram_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        )

    def get_by_id(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        )
