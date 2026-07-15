from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


class VpnPlanRepository:
    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        name: str,
        duration_days: int,
        price_rub: int,
        price_stars: int,
        max_devices: int = 3,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO vpn_plans (
                code, name, duration_days, price_rub, price_stars,
                max_devices, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                duration_days = excluded.duration_days,
                price_rub = excluded.price_rub,
                price_stars = excluded.price_stars,
                max_devices = excluded.max_devices,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (
                code,
                name,
                duration_days,
                price_rub,
                price_stars,
                max_devices,
                bool(is_active),
                now,
                now,
            ),
        )
        plan = self.get_by_code(conn, code)
        if plan is None:
            raise RuntimeError("Could not upsert VPN plan")
        return plan

    def get_by_id(
        self, conn: sqlite3.Connection, plan_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        )

    def get_by_code(
        self, conn: sqlite3.Connection, code: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_plans WHERE code = ?", (code,)
            ).fetchone()
        )

    def list_active(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_plans
                WHERE is_active = TRUE
                ORDER BY duration_days ASC, price_rub ASC
                """
            ).fetchall()
        )

    def set_active(
        self, conn: sqlite3.Connection, *, plan_id: int, is_active: bool
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE vpn_plans
            SET is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (bool(is_active), iso_now(), plan_id),
        )
        plan = self.get_by_id(conn, plan_id)
        if plan is None:
            raise RuntimeError("Could not update VPN plan")
        return plan
