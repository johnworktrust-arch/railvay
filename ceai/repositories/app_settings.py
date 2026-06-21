from __future__ import annotations

import sqlite3
from typing import Dict, Iterable

from ceai.time_utils import iso_now


class AppSettingsRepository:
    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        key: str,
        value: str,
        is_secret: bool = False,
    ) -> None:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO app_settings (key, value, is_secret, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
            """,
            (key, value, bool(is_secret), now, now),
        )

    def get_many(self, conn: sqlite3.Connection, keys: Iterable[str]) -> Dict[str, str]:
        key_list = list(keys)
        if not key_list:
            return {}
        placeholders = ", ".join("?" for _ in key_list)
        rows = conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
            tuple(key_list),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}
