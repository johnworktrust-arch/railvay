from __future__ import annotations

import sqlite3
from typing import Any, Dict, Tuple

from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class CoinTransactionRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        amount: int,
        type_: str,
        status: str,
        reason: str,
        idempotency_key: str,
        subscription_id: int | None = None,
        payment_id: int | None = None,
        generation_id: int | None = None,
    ) -> Tuple[Dict[str, Any], bool]:
        now = iso_now()
        completed_at = now if status == "completed" else None
        cursor = conn.execute(
            """
            INSERT INTO coin_transactions (
                user_id, subscription_id, payment_id, generation_id,
                amount, type, status, reason, idempotency_key,
                created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                user_id,
                subscription_id,
                payment_id,
                generation_id,
                amount,
                type_,
                status,
                reason,
                idempotency_key,
                now,
                completed_at,
            ),
        )
        id_row = cursor.fetchone()
        if id_row is not None:
            row = self.get_by_id(conn, int(id_row["id"]))
            if row is None:
                raise RuntimeError("Could not create coin transaction")
            return row, True

        existing = self.get_by_idempotency_key(conn, idempotency_key)
        if existing is None:
            raise RuntimeError("Could not load existing coin transaction")
        return existing, False

    def get_by_id(
        self, conn: sqlite3.Connection, transaction_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM coin_transactions WHERE id = ?", (transaction_id,)
            ).fetchone()
        )

    def get_by_idempotency_key(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM coin_transactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        )

    def balance_for_subscription(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS balance
            FROM coin_transactions
            WHERE subscription_id = ? AND status = 'completed'
            """,
            (subscription_id,),
        ).fetchone()
        return int(row["balance"] if row else 0)
