from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class TextChatRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        model_price_id: int,
        title: str,
        is_default: bool = False,
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO text_chats (
                user_id, model_price_id, title, is_default, is_active,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, TRUE, ?, ?)
            RETURNING id
            """,
            (user_id, model_price_id, title, bool(is_default), now, now),
        )
        row = cursor.fetchone()
        chat = self.get_by_id(conn, int(row["id"]))
        if chat is None:
            raise RuntimeError("Could not create text chat")
        return chat

    def get_by_id(
        self, conn: sqlite3.Connection, chat_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute("SELECT * FROM text_chats WHERE id = ?", (chat_id,)).fetchone()
        )

    def get_active_for_user(
        self, conn: sqlite3.Connection, *, user_id: int, chat_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM text_chats
                WHERE id = ? AND user_id = ? AND is_active = TRUE
                """,
                (chat_id, user_id),
            ).fetchone()
        )

    def find_active_by_title(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        model_price_id: int,
        title: str,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM text_chats
                WHERE user_id = ?
                  AND model_price_id = ?
                  AND LOWER(title) = LOWER(?)
                  AND is_active = TRUE
                LIMIT 1
                """,
                (user_id, model_price_id, title),
            ).fetchone()
        )

    def list_active_for_model(
        self, conn: sqlite3.Connection, *, user_id: int, model_price_id: int
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM text_chats
                WHERE user_id = ?
                  AND model_price_id = ?
                  AND is_active = TRUE
                ORDER BY
                    is_default DESC,
                    CASE title
                        WHEN 'Основной' THEN 1
                        WHEN 'Медицина' THEN 2
                        WHEN 'Работа' THEN 3
                        WHEN 'Психолог' THEN 4
                        WHEN 'Спорт' THEN 5
                        ELSE 100
                    END,
                    id ASC
                """,
                (user_id, model_price_id),
            ).fetchall()
        )

    def soft_delete(self, conn: sqlite3.Connection, chat_id: int) -> None:
        conn.execute(
            """
            UPDATE text_chats
            SET is_active = FALSE,
                updated_at = ?
            WHERE id = ?
            """,
            (iso_now(), chat_id),
        )
