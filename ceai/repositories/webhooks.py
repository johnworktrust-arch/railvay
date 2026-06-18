from __future__ import annotations

import sqlite3
from typing import Any, Dict, Tuple

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class WebhookLogRepository:
    def create_received(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        external_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        now = iso_now()
        try:
            cursor = conn.execute(
                """
                INSERT INTO webhook_logs (
                    provider, external_id, event_type, payload, status, created_at
                )
                VALUES (?, ?, ?, ?::jsonb, 'received', ?)
                RETURNING id
                """,
                (provider, external_id, event_type, dumps(payload), now),
            )
            id_row = cursor.fetchone()
            row = self.get_by_id(conn, int(id_row["id"]))
            if row is None:
                raise RuntimeError("Could not create webhook log")
            return row, True
        except sqlite3.IntegrityError:
            existing = self.get_by_key(conn, provider, external_id, event_type)
            if existing is None:
                raise
            return existing, False

    def get_by_id(
        self, conn: sqlite3.Connection, webhook_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM webhook_logs WHERE id = ?", (webhook_id,)
            ).fetchone()
        )

    def get_by_key(
        self,
        conn: sqlite3.Connection,
        provider: str,
        external_id: str,
        event_type: str,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM webhook_logs
                WHERE provider = ? AND external_id = ? AND event_type = ?
                """,
                (provider, external_id, event_type),
            ).fetchone()
        )

    def mark(
        self,
        conn: sqlite3.Connection,
        *,
        webhook_id: int,
        status: str,
        error_message: str | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE webhook_logs
            SET status = ?, error_message = ?, processed_at = ?
            WHERE id = ?
            """,
            (status, error_message, now, webhook_id),
        )
        webhook = self.get_by_id(conn, webhook_id)
        if webhook is None:
            raise RuntimeError("Could not update webhook log")
        return webhook
