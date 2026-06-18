from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceai.json_utils import dumps
from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class PaymentRepository:
    def create_pending(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        amount_rub: int,
        external_id: str,
        payment_url: str,
        provider: str = "mock",
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO payments (
                user_id, plan_id, provider, external_id, status,
                amount_rub, discount_rub, payment_url, meta, created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?::jsonb, ?)
            RETURNING id
            """,
            (
                user_id,
                plan_id,
                provider,
                external_id,
                amount_rub,
                payment_url,
                dumps({"kind": "mock_payment"}),
                now,
            ),
        )
        row = cursor.fetchone()
        payment = self.get_by_id(conn, int(row["id"]))
        if payment is None:
            raise RuntimeError("Could not create payment")
        return payment

    def get_by_id(self, conn: sqlite3.Connection, payment_id: int) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        )

    def get_by_external_id(
        self, conn: sqlite3.Connection, provider: str, external_id: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM payments WHERE provider = ? AND external_id = ?",
                (provider, external_id),
            ).fetchone()
        )

    def mark_paid(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        subscription_id: int,
        meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            UPDATE payments
            SET status = 'paid', subscription_id = ?, paid_at = ?, meta = ?::jsonb
            WHERE id = ?
            """,
            (subscription_id, now, dumps(meta), payment_id),
        )
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError("Could not mark payment paid")
        return payment
