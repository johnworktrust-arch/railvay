from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class ModelPriceRepository:
    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        model_key: str,
        display_name: str,
        generation_type: str,
        coins_cost: int,
        config: Dict[str, Any],
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO model_prices (
                provider, model_key, display_name, generation_type,
                coins_cost, is_active, config, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?)
            ON CONFLICT(provider, model_key) DO UPDATE SET
                display_name = excluded.display_name,
                generation_type = excluded.generation_type,
                coins_cost = excluded.coins_cost,
                is_active = excluded.is_active,
                config = excluded.config,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                model_key,
                display_name,
                generation_type,
                coins_cost,
                bool(is_active),
                dumps(config),
                now,
                now,
            ),
        )
        model = self.get_by_provider_key(conn, provider, model_key)
        if model is None:
            raise RuntimeError("Could not upsert model price")
        return model

    def list_active(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM model_prices
                WHERE is_active = TRUE
                ORDER BY
                    CASE generation_type
                        WHEN 'text' THEN 1
                        WHEN 'image' THEN 2
                        WHEN 'video' THEN 3
                        WHEN 'tts' THEN 4
                        ELSE 5
                    END,
                    coins_cost ASC
                """
            ).fetchall()
        )

    def get_by_id(
        self, conn: sqlite3.Connection, model_price_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM model_prices WHERE id = ?", (model_price_id,)
            ).fetchone()
        )

    def get_by_provider_key(
        self, conn: sqlite3.Connection, provider: str, model_key: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM model_prices WHERE provider = ? AND model_key = ?",
                (provider, model_key),
            ).fetchone()
        )
