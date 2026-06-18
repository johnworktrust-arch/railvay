from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class PlanRepository:
    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        name: str,
        price_rub: int,
        duration_days: int,
        coins_amount: int,
        features: Dict[str, Any],
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO plans (
                code, name, price_rub, duration_days, coins_amount,
                features, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                price_rub = excluded.price_rub,
                duration_days = excluded.duration_days,
                coins_amount = excluded.coins_amount,
                features = excluded.features,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (
                code,
                name,
                price_rub,
                duration_days,
                coins_amount,
                dumps(features),
                bool(is_active),
                now,
                now,
            ),
        )
        plan = self.get_by_code(conn, code)
        if plan is None:
            raise RuntimeError("Could not upsert plan")
        return plan

    def list_active(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM plans WHERE is_active = TRUE ORDER BY price_rub ASC"
            ).fetchall()
        )

    def get_by_code(self, conn: sqlite3.Connection, code: str) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM plans WHERE code = ?", (code,)
            ).fetchone()
        )

    def get_by_id(self, conn: sqlite3.Connection, plan_id: int) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        )
