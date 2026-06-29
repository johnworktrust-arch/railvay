from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceai.json_utils import dumps, loads_dict
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
        meta: Dict[str, Any] | None = None,
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
                dumps(meta or {"kind": f"{provider}_payment"}),
                now,
            ),
        )
        row = cursor.fetchone()
        payment = self.get_by_id(conn, int(row["id"]))
        if payment is None:
            raise RuntimeError("Could not create payment")
        return payment

    def get_by_id(
        self, conn: sqlite3.Connection, payment_id: int
    ) -> Dict[str, Any] | None:
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

    def latest_paid_with_plan_for_user(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT
                    pay.id,
                    pay.user_id,
                    pay.plan_id,
                    pay.subscription_id,
                    pay.promocode_id,
                    pay.provider,
                    pay.external_id,
                    pay.status,
                    pay.amount_rub,
                    pay.discount_rub,
                    pay.payment_url,
                    pay.meta,
                    pay.created_at,
                    pay.paid_at,
                    p.duration_days AS plan_duration_days,
                    p.coins_amount AS plan_coins_amount,
                    p.name AS plan_name,
                    p.code AS plan_code
                FROM payments pay
                JOIN plans p ON p.id = pay.plan_id
                WHERE pay.user_id = ? AND pay.status = 'paid'
                ORDER BY COALESCE(pay.paid_at, pay.created_at) DESC
                LIMIT 1
                """,
                (user_id,),
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

    def mark_status(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        status: str,
        meta: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError("Could not load payment")
        next_meta = meta if meta is not None else loads_dict(payment.get("meta"))
        conn.execute(
            """
            UPDATE payments
            SET status = ?,
                meta = ?::jsonb
            WHERE id = ?
            """,
            (status, dumps(next_meta), payment_id),
        )
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError("Could not update payment status")
        return payment

    def set_subscription_id(
        self, conn: sqlite3.Connection, *, payment_id: int, subscription_id: int
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE payments
            SET subscription_id = ?
            WHERE id = ?
            """,
            (subscription_id, payment_id),
        )
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError("Could not update payment subscription")
        return payment
