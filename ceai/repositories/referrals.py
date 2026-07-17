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

    def get_paid_vpn_payment(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment_id: int,
        provider: str,
        external_id: str,
    ) -> Dict[str, Any] | None:
        return self._get_vpn_payment_with_status(
            conn,
            vpn_payment_id=vpn_payment_id,
            provider=provider,
            external_id=external_id,
            status="paid",
        )

    def get_refunded_vpn_payment(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment_id: int,
        provider: str,
        external_id: str,
    ) -> Dict[str, Any] | None:
        return self._get_vpn_payment_with_status(
            conn,
            vpn_payment_id=vpn_payment_id,
            provider=provider,
            external_id=external_id,
            status="refunded",
        )

    def create_vpn_credit(
        self,
        conn: sqlite3.Connection,
        *,
        referrer_user_id: int,
        referred_user_id: int,
        vpn_payment_provider: str,
        vpn_payment_id: int,
        vpn_payment_external_id: str,
        amount_kopecks: int,
        rate_percent: int,
        idempotency_key: str,
    ) -> Tuple[Dict[str, Any], bool]:
        return self._create_vpn_source_transaction(
            conn,
            referrer_user_id=referrer_user_id,
            referred_user_id=referred_user_id,
            vpn_payment_provider=vpn_payment_provider,
            vpn_payment_id=vpn_payment_id,
            vpn_payment_external_id=vpn_payment_external_id,
            amount_kopecks=amount_kopecks,
            rate_percent=rate_percent,
            transaction_type="credit",
            reason="vpn_payment_referral_reward",
            idempotency_key=idempotency_key,
        )

    def create_vpn_chargeback_adjustment(
        self,
        conn: sqlite3.Connection,
        *,
        credit_transaction: Dict[str, Any],
        idempotency_key: str,
    ) -> Tuple[Dict[str, Any], bool]:
        amount_kopecks = int(credit_transaction["amount_kopecks"])
        if amount_kopecks <= 0 or credit_transaction.get("type") != "credit":
            raise ValueError("VPN referral credit is invalid")
        return self._create_vpn_source_transaction(
            conn,
            referrer_user_id=int(credit_transaction["referrer_user_id"]),
            referred_user_id=int(credit_transaction["referred_user_id"]),
            vpn_payment_provider=str(
                credit_transaction["vpn_payment_provider"]
            ),
            vpn_payment_id=int(credit_transaction["vpn_payment_id"]),
            vpn_payment_external_id=str(
                credit_transaction["vpn_payment_external_id"]
            ),
            amount_kopecks=-amount_kopecks,
            rate_percent=int(credit_transaction["rate_percent"]),
            transaction_type="adjustment",
            reason="vpn_payment_referral_chargeback",
            idempotency_key=idempotency_key,
        )

    def get_vpn_source_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment_provider: str,
        vpn_payment_id: int,
        transaction_type: str,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM referral_transactions
                WHERE vpn_payment_provider = ?
                  AND vpn_payment_id = ?
                  AND type = ?
                """,
                (
                    vpn_payment_provider,
                    vpn_payment_id,
                    transaction_type,
                ),
            ).fetchone()
        )

    def _get_vpn_payment_with_status(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment_id: int,
        provider: str,
        external_id: str,
        status: str,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_payments
                WHERE id = ?
                  AND provider = ?
                  AND external_id = ?
                  AND status = ?
                """,
                (vpn_payment_id, provider, external_id, status),
            ).fetchone()
        )

    def _create_vpn_source_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        referrer_user_id: int,
        referred_user_id: int,
        vpn_payment_provider: str,
        vpn_payment_id: int,
        vpn_payment_external_id: str,
        amount_kopecks: int,
        rate_percent: int,
        transaction_type: str,
        reason: str,
        idempotency_key: str,
    ) -> Tuple[Dict[str, Any], bool]:
        if transaction_type not in {"credit", "adjustment"}:
            raise ValueError("Unsupported VPN referral transaction type")
        if referrer_user_id <= 0 or referred_user_id <= 0:
            raise ValueError("VPN referral user ID must be greater than zero")
        if referrer_user_id == referred_user_id:
            raise ValueError("Self-referral transactions are not allowed")
        if vpn_payment_id <= 0:
            raise ValueError("VPN payment ID must be greater than zero")
        if (
            not vpn_payment_provider.strip()
            or not vpn_payment_external_id.strip()
        ):
            raise ValueError("VPN payment source must not be empty")
        if amount_kopecks == 0 or (
            transaction_type == "credit" and amount_kopecks < 0
        ) or (
            transaction_type == "adjustment" and amount_kopecks > 0
        ):
            raise ValueError("VPN referral transaction amount has invalid sign")
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO referral_transactions (
                referrer_user_id, referred_user_id, payment_id,
                vpn_payment_provider, vpn_payment_id,
                vpn_payment_external_id, amount_kopecks, rate_percent,
                type, status, reason, idempotency_key, created_at,
                completed_at
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                referrer_user_id,
                referred_user_id,
                vpn_payment_provider,
                vpn_payment_id,
                vpn_payment_external_id,
                amount_kopecks,
                rate_percent,
                transaction_type,
                reason,
                idempotency_key,
                now,
                now,
            ),
        )
        id_row = cursor.fetchone()
        if id_row is not None:
            created = self.get_transaction_by_id(conn, int(id_row["id"]))
            if created is None:
                raise RuntimeError("Could not create VPN referral transaction")
            return created, True

        existing = self.get_vpn_source_transaction(
            conn,
            vpn_payment_provider=vpn_payment_provider,
            vpn_payment_id=vpn_payment_id,
            transaction_type=transaction_type,
        )
        if existing is None:
            existing = self.get_transaction_by_idempotency_key(
                conn, idempotency_key
            )
        if existing is None:
            raise RuntimeError("Could not load existing VPN referral transaction")
        expected = {
            "referrer_user_id": referrer_user_id,
            "referred_user_id": referred_user_id,
            "vpn_payment_provider": vpn_payment_provider,
            "vpn_payment_id": vpn_payment_id,
            "vpn_payment_external_id": vpn_payment_external_id,
            "amount_kopecks": amount_kopecks,
            "rate_percent": rate_percent,
            "type": transaction_type,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        for field, value in expected.items():
            if existing.get(field) != value:
                raise RuntimeError(
                    "Existing VPN referral transaction does not match its source"
                )
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
