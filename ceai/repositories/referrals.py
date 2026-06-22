from __future__ import annotations

import sqlite3
from typing import Any, Dict, Tuple

from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class ReferralRepository:
    def get_user_by_code(
        self, conn: sqlite3.Connection, referral_code: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE referral_code = ?",
                (referral_code,),
            ).fetchone()
        )

    def assign_referrer(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        referrer_user_id: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE users
            SET referred_by_user_id = ?
            WHERE id = ? AND referred_by_user_id IS NULL AND id <> ?
            """,
            (referrer_user_id, user_id, referrer_user_id),
        )
        return int(cursor.rowcount or 0) > 0

    def invited_count(self, conn: sqlite3.Connection, user_id: int) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM users WHERE referred_by_user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def balance_kopecks(self, conn: sqlite3.Connection, user_id: int) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_kopecks), 0) AS balance
            FROM referral_transactions
            WHERE referrer_user_id = ? AND status = 'completed'
            """,
            (user_id,),
        ).fetchone()
        return int(row["balance"] if row else 0)

    def get_payout_settings(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM referral_payout_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        )

    def upsert_payout_settings(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        withdrawal_method: str,
        requisites: str,
    ) -> Dict[str, Any]:
        now = iso_now()
        conn.execute(
            """
            INSERT INTO referral_payout_settings (
                user_id, withdrawal_method, requisites, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                withdrawal_method = excluded.withdrawal_method,
                requisites = excluded.requisites,
                updated_at = excluded.updated_at
            """,
            (user_id, withdrawal_method, requisites, now, now),
        )
        settings = self.get_payout_settings(conn, user_id)
        if settings is None:
            raise RuntimeError("Could not save referral payout settings")
        return settings

    def create_credit(
        self,
        conn: sqlite3.Connection,
        *,
        referrer_user_id: int,
        referred_user_id: int,
        payment_id: int,
        amount_kopecks: int,
        rate_percent: int,
        idempotency_key: str,
    ) -> Tuple[Dict[str, Any], bool]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO referral_transactions (
                referrer_user_id, referred_user_id, payment_id, amount_kopecks,
                rate_percent, type, status, reason, idempotency_key,
                created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, 'credit', 'completed', 'payment_referral_reward',
                    ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                referrer_user_id,
                referred_user_id,
                payment_id,
                amount_kopecks,
                rate_percent,
                idempotency_key,
                now,
                now,
            ),
        )
        id_row = cursor.fetchone()
        if id_row is not None:
            created = self.get_transaction_by_id(conn, int(id_row["id"]))
            if created is None:
                raise RuntimeError("Could not create referral transaction")
            return created, True

        existing = self.get_transaction_by_idempotency_key(conn, idempotency_key)
        if existing is None:
            raise RuntimeError("Could not load existing referral transaction")
        return existing, False

    def get_transaction_by_id(
        self, conn: sqlite3.Connection, transaction_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM referral_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
        )

    def get_transaction_by_idempotency_key(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM referral_transactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        )
