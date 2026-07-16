from __future__ import annotations

import secrets
import sqlite3
from typing import Any, Dict, Tuple

from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


ADMIN_DEMO_PROVIDER = "admin_demo"
RUB_CURRENCY = "RUB"


class VpnPaymentRepository:
    def create_or_get_pending_admin_demo(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        amount_rub: int,
        duration_days: int,
        payment_method: str,
    ) -> Tuple[Dict[str, Any], bool]:
        method = self._normalize_payment_method(payment_method)
        if user_id <= 0:
            raise ValueError("user_id must be greater than zero")
        if plan_id <= 0:
            raise ValueError("plan_id must be greater than zero")
        if amount_rub < 0:
            raise ValueError("amount_rub must not be negative")
        if duration_days <= 0:
            raise ValueError("duration_days must be greater than zero")

        now = iso_now()
        external_id = f"vpn_admin_demo_{secrets.token_urlsafe(18)}"
        cursor = conn.execute(
            """
            INSERT INTO vpn_payments (
                user_id, vpn_plan_id, vpn_subscription_id,
                provider, external_id, payment_method, status,
                amount_rub, duration_days, currency,
                created_at, updated_at, paid_at
            )
            VALUES (
                ?, ?, NULL, 'admin_demo', ?, ?, 'pending', ?, ?,
                'RUB', ?, ?, NULL
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                user_id,
                plan_id,
                external_id,
                method,
                amount_rub,
                duration_days,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            payment = self.get_by_id(conn, int(row["id"]))
            if payment is None:
                raise RuntimeError("Could not create VPN admin demo payment")
            return payment, True

        payment = row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_payments
                WHERE user_id = ?
                  AND vpn_plan_id = ?
                  AND provider = 'admin_demo'
                  AND payment_method = ?
                  AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, plan_id, method),
            ).fetchone()
        )
        if payment is None:
            raise RuntimeError("Could not load pending VPN admin demo payment")
        # A pending order is an immutable price/duration snapshot. If the plan
        # changes later, the existing order keeps the terms shown to the user.
        return payment, False

    def get_by_id(
        self, conn: sqlite3.Connection, payment_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_payments WHERE id = ?", (payment_id,)
            ).fetchone()
        )

    def get_for_user(
        self,
        conn: sqlite3.Connection,
        payment_id: int,
        user_id: int,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_payments
                WHERE id = ? AND user_id = ?
                """,
                (payment_id, user_id),
            ).fetchone()
        )

    def mark_admin_demo_paid(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        user_id: int,
    ) -> Tuple[Dict[str, Any], bool]:
        now = iso_now()
        cursor = conn.execute(
            """
            UPDATE vpn_payments
            SET status = 'paid', paid_at = ?, updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND provider = 'admin_demo'
              AND status = 'pending'
            RETURNING id
            """,
            (now, now, payment_id, user_id),
        )
        row = cursor.fetchone()
        if row is not None:
            return self._require_by_id(conn, payment_id, "mark paid"), True

        payment = self._require_by_id(conn, payment_id, "mark paid")
        if int(payment["user_id"]) != int(user_id):
            raise ValueError("VPN payment belongs to another user")
        if payment["provider"] != ADMIN_DEMO_PROVIDER:
            raise ValueError("VPN payment is not an admin demo payment")
        if payment["status"] == "paid":
            return payment, False
        raise ValueError(
            f"VPN admin demo payment cannot be paid from status {payment['status']}"
        )

    def link_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        user_id: int,
        subscription_id: int,
    ) -> Dict[str, Any]:
        cursor = conn.execute(
            """
            UPDATE vpn_payments
            SET vpn_subscription_id = ?, updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND provider = 'admin_demo'
              AND status = 'paid'
              AND (
                  vpn_subscription_id IS NULL
                  OR vpn_subscription_id = ?
              )
              AND EXISTS (
                  SELECT 1
                  FROM vpn_subscriptions subscription
                  WHERE subscription.id = ?
                    AND subscription.user_id = vpn_payments.user_id
                    AND subscription.plan_id = vpn_payments.vpn_plan_id
                    AND subscription.billing_kind = 'paid'
              )
            RETURNING id
            """,
            (
                subscription_id,
                iso_now(),
                payment_id,
                user_id,
                subscription_id,
                subscription_id,
            ),
        )
        if cursor.fetchone() is not None:
            return self._require_by_id(conn, payment_id, "link subscription to")

        payment = self._require_by_id(conn, payment_id, "link subscription to")
        if int(payment["user_id"]) != int(user_id):
            raise ValueError("VPN payment belongs to another user")
        if payment["provider"] != ADMIN_DEMO_PROVIDER:
            raise ValueError("VPN payment is not an admin demo payment")
        if payment["status"] != "paid":
            raise ValueError("Only a paid VPN payment can be linked")
        if payment.get("vpn_subscription_id") is not None:
            raise ValueError("VPN payment is already linked to another subscription")
        raise ValueError(
            "VPN subscription must match the payment plan and user"
        )

    def _require_by_id(
        self, conn: sqlite3.Connection, payment_id: int, action: str
    ) -> Dict[str, Any]:
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError(f"Could not {action} VPN payment")
        return payment

    @staticmethod
    def _normalize_payment_method(payment_method: str) -> str:
        method = payment_method.strip().lower()
        if not method:
            raise ValueError("payment_method must not be empty")
        if len(method) > 64:
            raise ValueError("payment_method is too long")
        return method


__all__ = ["ADMIN_DEMO_PROVIDER", "RUB_CURRENCY", "VpnPaymentRepository"]
