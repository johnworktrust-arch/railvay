from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class BotSessionRepository:
    def get_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM bot_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        )

    def set_state(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        state: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO bot_sessions (user_id, state, payload, created_at, updated_at)
            VALUES (?, ?, ?::jsonb, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (user_id, state, dumps(payload or {}), now, now),
        )
        session = self.get_for_user(conn, user_id)
        if session is None:
            raise RuntimeError("Could not set bot session")
        return session

    def clear(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
        return self.set_state(conn, user_id=user_id, state="idle", payload={})
