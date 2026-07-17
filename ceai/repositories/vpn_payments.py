from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now


ADMIN_DEMO_PROVIDER = "admin_demo"
PLATEGA_PROVIDER = "platega"
RUB_CURRENCY = "RUB"

_SUPPORTED_PROVIDERS = frozenset({ADMIN_DEMO_PROVIDER, PLATEGA_PROVIDER})
_PROVIDER_TERMINAL_TRANSITIONS = {
    "failed": frozenset({"pending"}),
    "cancelled": frozenset({"pending"}),
    # Platega may report CHARGEBACKED before a CONFIRMED callback has been
    # processed locally. Closing that still-pending order is important because
    # the partial unique index must not wedge all future checkout attempts.
    "refunded": frozenset({"failed", "pending", "paid"}),
}


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
        self._validate_snapshot(
            user_id=user_id,
            plan_id=plan_id,
            amount_rub=amount_rub,
            duration_days=duration_days,
        )

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

        payment = self._get_pending_for_terms(
            conn,
            user_id=user_id,
            plan_id=plan_id,
            provider=ADMIN_DEMO_PROVIDER,
            payment_method=method,
        )
        if payment is None:
            raise RuntimeError("Could not load pending VPN admin demo payment")
        # A pending order is an immutable price/duration snapshot. If the plan
        # changes later, the existing order keeps the terms shown to the user.
        return payment, False

    def create_or_get_pending_platega(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        amount_rub: int,
        duration_days: int,
        payment_method: str,
        request_external_id: str,
    ) -> Tuple[Dict[str, Any], bool]:
        """Reserve one Platega order before making the remote API request.

        ``request_external_id`` is a caller-generated, collision-resistant local
        placeholder. ``attach_platega_transaction`` later replaces it with the
        provider transaction ID using a compare-and-set update. This keeps the
        remote request outside the database transaction without ever attaching
        its response to a different pending order.
        """

        method = self._normalize_payment_method(payment_method)
        placeholder = self._normalize_external_id(request_external_id)
        self._validate_snapshot(
            user_id=user_id,
            plan_id=plan_id,
            amount_rub=amount_rub,
            duration_days=duration_days,
        )

        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO vpn_payments (
                user_id, vpn_plan_id, vpn_subscription_id,
                provider, external_id, payment_method, status,
                amount_rub, duration_days, currency,
                created_at, updated_at, paid_at, payment_url, expires_at
            )
            VALUES (
                ?, ?, NULL, 'platega', ?, ?, 'pending', ?, ?,
                'RUB', ?, ?, NULL, NULL, NULL
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                user_id,
                plan_id,
                placeholder,
                method,
                amount_rub,
                duration_days,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            return self._require_by_id(
                conn, int(row["id"]), "create Platega"
            ), True

        payment = self._get_pending_for_terms(
            conn,
            user_id=user_id,
            plan_id=plan_id,
            provider=PLATEGA_PROVIDER,
            payment_method=method,
        )
        if payment is None:
            # The conflict was on (provider, external_id), not the partial
            # pending-order index. Treat a placeholder collision as an error;
            # never return an unrelated payment.
            raise RuntimeError("Could not create pending Platega payment")
        return payment, False

    def attach_platega_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        user_id: int,
        expected_external_id: str,
        external_id: str,
        payment_url: str,
        expires_at: str | None,
    ) -> Dict[str, Any]:
        placeholder = self._normalize_external_id(expected_external_id)
        provider_external_id = self._normalize_external_id(external_id)
        url = self._normalize_payment_url(payment_url)
        normalized_expires_at = self._normalize_expires_at(expires_at)

        cursor = conn.execute(
            """
            UPDATE vpn_payments
            SET external_id = ?, payment_url = ?, expires_at = ?, updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND provider = 'platega'
              AND status = 'pending'
              AND external_id = ?
              AND payment_url IS NULL
            RETURNING id
            """,
            (
                provider_external_id,
                url,
                normalized_expires_at,
                iso_now(),
                payment_id,
                user_id,
                placeholder,
            ),
        )
        if cursor.fetchone() is not None:
            return self._require_by_id(
                conn, payment_id, "attach Platega transaction to"
            )

        payment = self._require_by_id(
            conn, payment_id, "attach Platega transaction to"
        )
        self._validate_payment_identity(
            payment,
            expected_provider=PLATEGA_PROVIDER,
            expected_external_id=None,
            user_id=user_id,
        )
        if (
            payment.get("external_id") == provider_external_id
            and payment.get("payment_url") == url
            and payment.get("expires_at") == normalized_expires_at
        ):
            return payment
        if payment.get("status") != "pending":
            raise ValueError("Only a pending Platega payment can be attached")
        if payment.get("payment_url") is not None:
            raise ValueError("Platega payment transaction is already attached")
        raise ValueError("Platega payment placeholder does not match")

    def get_by_id(
        self, conn: sqlite3.Connection, payment_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_payments WHERE id = ?", (payment_id,)
            ).fetchone()
        )

    def get_by_provider_external_id(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        external_id: str,
    ) -> Dict[str, Any] | None:
        normalized_provider = self._normalize_provider(provider)
        normalized_external_id = self._normalize_external_id(external_id)
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_payments
                WHERE provider = ? AND external_id = ?
                """,
                (normalized_provider, normalized_external_id),
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

    def list_attached_pending_platega_for_reconciliation(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int,
    ) -> list[Dict[str, Any]]:
        if limit <= 0:
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM vpn_payments
            WHERE provider = 'platega'
              AND status = 'pending'
              AND payment_url IS NOT NULL
              AND payment_url <> ''
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows_to_dicts(rows)

    def list_recent_failed_platega_for_reconciliation(
        self,
        conn: sqlite3.Connection,
        *,
        failed_since: str,
        after_id: int,
        limit: int,
    ) -> list[Dict[str, Any]]:
        """Return a circular slice of recently synthetic-failed orders.

        ``updated_at`` is the time the expired order was closed as failed. It
        deliberately is not touched while Platega still reports PENDING, so a
        transaction leaves this short race-recovery window deterministically.
        The circular ID ordering prevents one provider error from monopolising
        the bounded maintenance batch.
        """

        if limit <= 0:
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM vpn_payments
            WHERE provider = 'platega'
              AND status = 'failed'
              AND payment_url IS NOT NULL
              AND payment_url <> ''
              AND updated_at >= ?
            ORDER BY
              CASE WHEN id > ? THEN 0 ELSE 1 END ASC,
              id ASC
            LIMIT ?
            """,
            (failed_since, after_id, limit),
        ).fetchall()
        return rows_to_dicts(rows)

    def list_recent_paid_platega_for_reconciliation(
        self,
        conn: sqlite3.Connection,
        *,
        paid_since: str,
        after_id: int,
        limit: int,
    ) -> list[Dict[str, Any]]:
        """Return a circular slice of paid orders inside the dispute horizon."""

        if limit <= 0:
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM vpn_payments
            WHERE provider = 'platega'
              AND status = 'paid'
              AND payment_url IS NOT NULL
              AND payment_url <> ''
              AND paid_at IS NOT NULL
              AND paid_at >= ?
            ORDER BY
              CASE WHEN id > ? THEN 0 ELSE 1 END ASC,
              id ASC
            LIMIT ?
            """,
            (paid_since, after_id, limit),
        ).fetchall()
        return rows_to_dicts(rows)

    def touch_pending_platega_reconciliation(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        external_id: str,
    ) -> bool:
        """Move an attempted pending order to the back of the polling queue."""

        normalized_external_id = self._normalize_external_id(external_id)
        cursor = conn.execute(
            """
            UPDATE vpn_payments
            SET updated_at = ?
            WHERE id = ?
              AND provider = 'platega'
              AND status = 'pending'
              AND external_id = ?
              AND payment_url IS NOT NULL
            RETURNING id
            """,
            (iso_now(), payment_id, normalized_external_id),
        )
        return cursor.fetchone() is not None

    def count_other_paid_for_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        excluding_payment_id: int,
    ) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM vpn_payments
            WHERE vpn_subscription_id = ?
              AND id <> ?
              AND status = 'paid'
            """,
            (subscription_id, excluding_payment_id),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def get_latest_other_paid_for_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        excluding_payment_id: int,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_payments
                WHERE vpn_subscription_id = ?
                  AND id <> ?
                  AND status = 'paid'
                ORDER BY paid_at DESC, id DESC
                LIMIT 1
                """,
                (subscription_id, excluding_payment_id),
            ).fetchone()
        )

    def mark_paid(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        expected_provider: str,
        expected_external_id: str | None = None,
        user_id: int | None = None,
    ) -> Tuple[Dict[str, Any], bool]:
        provider = self._normalize_provider(expected_provider)
        external_id = (
            self._normalize_external_id(expected_external_id)
            if expected_external_id is not None
            else None
        )
        payable_statuses = (
            frozenset({"pending", "failed"})
            if provider == PLATEGA_PROVIDER
            else frozenset({"pending"})
        )
        clauses = ["id = ?", "provider = ?"]
        params: list[Any] = [iso_now(), iso_now(), payment_id, provider]
        if len(payable_statuses) == 1:
            clauses.append("status = ?")
            params.append(next(iter(payable_statuses)))
        else:
            placeholders = ", ".join("?" for _ in payable_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(payable_statuses))
        if external_id is not None:
            clauses.append("external_id = ?")
            params.append(external_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)

        cursor = conn.execute(
            f"""
            UPDATE vpn_payments
            SET status = 'paid', paid_at = ?, updated_at = ?
            WHERE {' AND '.join(clauses)}
            RETURNING id
            """,
            tuple(params),
        )
        if cursor.fetchone() is not None:
            return self._require_by_id(conn, payment_id, "mark paid"), True

        payment = self._require_by_id(conn, payment_id, "mark paid")
        self._validate_payment_identity(
            payment,
            expected_provider=provider,
            expected_external_id=external_id,
            user_id=user_id,
        )
        if payment["status"] == "paid":
            return payment, False
        raise ValueError(
            f"VPN {provider} payment cannot be paid from status "
            f"{payment['status']}"
        )

    def mark_admin_demo_paid(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        user_id: int,
    ) -> Tuple[Dict[str, Any], bool]:
        return self.mark_paid(
            conn,
            payment_id=payment_id,
            user_id=user_id,
            expected_provider=ADMIN_DEMO_PROVIDER,
        )

    def mark_provider_status(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        expected_provider: str,
        status: str,
        expected_external_id: str | None = None,
        user_id: int | None = None,
    ) -> Tuple[Dict[str, Any], bool]:
        provider = self._normalize_provider(expected_provider)
        target_status = status.strip().lower()
        allowed_from = _PROVIDER_TERMINAL_TRANSITIONS.get(target_status)
        if allowed_from is None:
            raise ValueError("Unsupported VPN provider payment status")
        external_id = (
            self._normalize_external_id(expected_external_id)
            if expected_external_id is not None
            else None
        )

        clauses = ["id = ?", "provider = ?"]
        params: list[Any] = [target_status, iso_now(), payment_id, provider]
        if len(allowed_from) == 1:
            clauses.append("status = ?")
            params.append(next(iter(allowed_from)))
        else:
            placeholders = ", ".join("?" for _ in allowed_from)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(allowed_from))
        if external_id is not None:
            clauses.append("external_id = ?")
            params.append(external_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)

        cursor = conn.execute(
            f"""
            UPDATE vpn_payments
            SET status = ?, updated_at = ?
            WHERE {' AND '.join(clauses)}
            RETURNING id
            """,
            tuple(params),
        )
        if cursor.fetchone() is not None:
            return self._require_by_id(conn, payment_id, "update status for"), True

        payment = self._require_by_id(conn, payment_id, "update status for")
        self._validate_payment_identity(
            payment,
            expected_provider=provider,
            expected_external_id=external_id,
            user_id=user_id,
        )
        if payment["status"] == target_status:
            return payment, False
        raise ValueError(
            f"VPN {provider} payment cannot move from status "
            f"{payment['status']} to {target_status}"
        )

    def link_subscription(
        self,
        conn: sqlite3.Connection,
        *,
        payment_id: int,
        user_id: int,
        subscription_id: int,
        expected_provider: str = ADMIN_DEMO_PROVIDER,
    ) -> Dict[str, Any]:
        provider = self._normalize_provider(expected_provider)
        cursor = conn.execute(
            """
            UPDATE vpn_payments
            SET vpn_subscription_id = ?, updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND provider = ?
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
                provider,
                subscription_id,
                subscription_id,
            ),
        )
        if cursor.fetchone() is not None:
            return self._require_by_id(conn, payment_id, "link subscription to")

        payment = self._require_by_id(conn, payment_id, "link subscription to")
        self._validate_payment_identity(
            payment,
            expected_provider=provider,
            expected_external_id=None,
            user_id=user_id,
        )
        if payment["status"] != "paid":
            raise ValueError("Only a paid VPN payment can be linked")
        linked_id = payment.get("vpn_subscription_id")
        if linked_id is not None and int(linked_id) != int(subscription_id):
            raise ValueError("VPN payment is already linked to another subscription")
        raise ValueError("VPN subscription must match the payment plan and user")

    def _get_pending_for_terms(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        plan_id: int,
        provider: str,
        payment_method: str,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_payments
                WHERE user_id = ?
                  AND vpn_plan_id = ?
                  AND provider = ?
                  AND payment_method = ?
                  AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, plan_id, provider, payment_method),
            ).fetchone()
        )

    def _require_by_id(
        self, conn: sqlite3.Connection, payment_id: int, action: str
    ) -> Dict[str, Any]:
        payment = self.get_by_id(conn, payment_id)
        if payment is None:
            raise RuntimeError(f"Could not {action} VPN payment")
        return payment

    @staticmethod
    def _validate_snapshot(
        *, user_id: int, plan_id: int, amount_rub: int, duration_days: int
    ) -> None:
        if user_id <= 0:
            raise ValueError("user_id must be greater than zero")
        if plan_id <= 0:
            raise ValueError("plan_id must be greater than zero")
        if amount_rub < 0:
            raise ValueError("amount_rub must not be negative")
        if duration_days <= 0:
            raise ValueError("duration_days must be greater than zero")

    @staticmethod
    def _normalize_payment_method(payment_method: str) -> str:
        method = payment_method.strip().lower()
        if not method:
            raise ValueError("payment_method must not be empty")
        if len(method) > 64:
            raise ValueError("payment_method is too long")
        return method

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = provider.strip().lower()
        if normalized not in _SUPPORTED_PROVIDERS:
            raise ValueError("Unsupported VPN payment provider")
        return normalized

    @staticmethod
    def _normalize_external_id(external_id: str) -> str:
        normalized = external_id.strip()
        if not normalized:
            raise ValueError("external_id must not be empty")
        if len(normalized) > 255:
            raise ValueError("external_id is too long")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("external_id contains control characters")
        return normalized

    @staticmethod
    def _normalize_payment_url(payment_url: str) -> str:
        normalized = payment_url.strip()
        if not normalized or len(normalized) > 2048:
            raise ValueError("payment_url is invalid")
        try:
            parsed = urlsplit(normalized)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("payment_url is invalid") from exc
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() != "pay.platega.io"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("payment_url must be a trusted Platega HTTPS URL")
        return normalized

    @staticmethod
    def _normalize_expires_at(expires_at: str | None) -> str | None:
        if expires_at is None:
            return None
        normalized = expires_at.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("expires_at is invalid")
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO 8601 timestamp") from exc
        if parsed.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return parsed.isoformat()

    @staticmethod
    def _validate_payment_identity(
        payment: Dict[str, Any],
        *,
        expected_provider: str,
        expected_external_id: str | None,
        user_id: int | None,
    ) -> None:
        if user_id is not None and int(payment["user_id"]) != int(user_id):
            raise ValueError("VPN payment belongs to another user")
        if payment["provider"] != expected_provider:
            raise ValueError("VPN payment belongs to another provider")
        if (
            expected_external_id is not None
            and payment["external_id"] != expected_external_id
        ):
            raise ValueError("VPN payment external ID does not match")


__all__ = [
    "ADMIN_DEMO_PROVIDER",
    "PLATEGA_PROVIDER",
    "RUB_CURRENCY",
    "VpnPaymentRepository",
]
