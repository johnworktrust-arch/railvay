from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class GenerationRepository:
    def create_pending(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        model_price_id: int,
        generation_type: str,
        provider: str,
        prompt: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO generations (
                user_id, model_price_id, generation_type, provider,
                status, prompt, created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?::jsonb, ?)
            RETURNING id
            """,
            (user_id, model_price_id, generation_type, provider, dumps(prompt), now),
        )
        row = cursor.fetchone()
        generation = self.get_by_id(conn, int(row["id"]))
        if generation is None:
            raise RuntimeError("Could not create generation")
        return generation

    def get_by_id(
        self, conn: sqlite3.Connection, generation_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
        )

    def get_for_user(
        self, conn: sqlite3.Connection, *, user_id: int, generation_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT
                    g.*,
                    mp.display_name AS model_display_name,
                    mp.coins_cost AS model_coins_cost
                FROM generations g
                JOIN model_prices mp ON mp.id = g.model_price_id
                WHERE g.user_id = ? AND g.id = ?
                """,
                (user_id, generation_id),
            ).fetchone()
        )

    def mark_processing(
        self,
        conn: sqlite3.Connection,
        *,
        generation_id: int,
        subscription_id: int,
        coins_reserved: int,
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE generations
            SET status = 'processing',
                subscription_id = ?,
                coins_reserved = ?
            WHERE id = ?
            """,
            (subscription_id, coins_reserved, generation_id),
        )
        generation = self.get_by_id(conn, generation_id)
        if generation is None:
            raise RuntimeError("Could not mark generation processing")
        return generation

    def mark_completed(
        self,
        conn: sqlite3.Connection,
        *,
        generation_id: int,
        result: Dict[str, Any],
        provider_job_id: str,
        coins_charged: int,
        provider_cost_amount: float,
        provider_cost_currency: str,
        duration_seconds: int | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE generations
            SET status = 'completed',
                result = ?::jsonb,
                provider_job_id = ?,
                coins_charged = ?,
                provider_cost_amount = ?,
                provider_cost_currency = ?,
                duration_seconds = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                dumps(result),
                provider_job_id,
                coins_charged,
                provider_cost_amount,
                provider_cost_currency,
                duration_seconds,
                now,
                generation_id,
            ),
        )
        generation = self.get_by_id(conn, generation_id)
        if generation is None:
            raise RuntimeError("Could not mark generation completed")
        return generation

    def update_result(
        self,
        conn: sqlite3.Connection,
        *,
        generation_id: int,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE generations
            SET result = ?::jsonb
            WHERE id = ?
            """,
            (dumps(result), generation_id),
        )
        generation = self.get_by_id(conn, generation_id)
        if generation is None:
            raise RuntimeError("Could not update generation result")
        return generation

    def mark_failed(
        self,
        conn: sqlite3.Connection,
        *,
        generation_id: int,
        error_message: str,
        subscription_id: int | None = None,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE generations
            SET status = 'failed',
                subscription_id = COALESCE(?, subscription_id),
                error_message = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (subscription_id, error_message, now, generation_id),
        )
        generation = self.get_by_id(conn, generation_id)
        if generation is None:
            raise RuntimeError("Could not mark generation failed")
        return generation

    def count_for_user(self, conn: sqlite3.Connection, *, user_id: int) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM generations WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["total"] if row else 0)

    def list_recent_for_user(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT
                    g.*,
                    mp.display_name AS model_display_name,
                    mp.coins_cost AS model_coins_cost
                FROM generations g
                JOIN model_prices mp ON mp.id = g.model_price_id
                WHERE g.user_id = ?
                ORDER BY g.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
        )
