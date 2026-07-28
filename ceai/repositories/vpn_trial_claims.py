from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Tuple

from ceai.repositories.base import row_to_dict
from ceai.time_utils import iso_now


class VpnTrialClaimRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
        channel: str,
        status: str = "pending",
    ) -> Tuple[Dict[str, Any], bool]:
        self._validate_trial_subscription(
            conn,
            user_id=user_id,
            subscription_id=subscription_id,
        )
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO vpn_trial_claims (
                user_id, subscription_id, subscription_kind, channel, status, claimed_at,
                created_at, updated_at
            )
            VALUES (?, ?, 'trial', ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            RETURNING id
            """,
            (user_id, subscription_id, channel, status, now, now, now),
        )
        row = cursor.fetchone()
        if row is not None:
            claim = self.get_by_id(conn, int(row["id"]))
            if claim is None:
                raise RuntimeError("Could not create VPN trial claim")
            return claim, True

        claim = self.get_by_user_id(conn, user_id)
        if claim is None:
            raise RuntimeError("Could not load existing VPN trial claim")
        return claim, False

    def get_by_id(
        self, conn: sqlite3.Connection, claim_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_trial_claims WHERE id = ?", (claim_id,)
            ).fetchone()
        )

    def get_by_user_id(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_trial_claims WHERE user_id = ?", (user_id,)
            ).fetchone()
        )

    def get_by_subscription_id(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_trial_claims WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        )

    def claim_due_expiry_reminders(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        remind_by: str,
        stale_before: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        candidates = conn.execute(
            """
            SELECT
                claim.id AS claim_id,
                claim.subscription_id,
                subscription.ends_at,
                subscription.plan_id,
                user.telegram_id
            FROM vpn_trial_claims claim
            JOIN vpn_subscriptions subscription
              ON subscription.id = claim.subscription_id
            JOIN users user
              ON user.id = claim.user_id
            WHERE claim.status = 'provisioned'
              AND claim.expiry_reminder_sent_at IS NULL
              AND (
                    claim.expiry_reminder_claimed_at IS NULL
                    OR claim.expiry_reminder_claimed_at <= ?
              )
              AND subscription.status = 'active'
              AND subscription.kind = 'trial'
              AND subscription.billing_kind = 'trial'
              AND subscription.ends_at > ?
              AND subscription.ends_at <= ?
            ORDER BY subscription.ends_at ASC, claim.id ASC
            LIMIT ?
            """,
            (stale_before, now, remind_by, limit),
        ).fetchall()

        claimed: List[Dict[str, Any]] = []
        for candidate in candidates:
            cursor = conn.execute(
                """
                UPDATE vpn_trial_claims
                SET expiry_reminder_claimed_at = ?, updated_at = ?
                WHERE id = ?
                  AND expiry_reminder_sent_at IS NULL
                  AND (
                        expiry_reminder_claimed_at IS NULL
                        OR expiry_reminder_claimed_at <= ?
                  )
                """,
                (now, now, int(candidate["claim_id"]), stale_before),
            )
            if cursor.rowcount == 1:
                claimed.append(row_to_dict(candidate) or {})
        return claimed

    def mark_expiry_reminder_sent(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: int,
        sent_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE vpn_trial_claims
            SET expiry_reminder_sent_at = ?,
                expiry_reminder_claimed_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (sent_at, sent_at, claim_id),
        )

    def release_expiry_reminder(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: int,
    ) -> None:
        conn.execute(
            """
            UPDATE vpn_trial_claims
            SET expiry_reminder_claimed_at = NULL, updated_at = ?
            WHERE id = ?
              AND expiry_reminder_sent_at IS NULL
            """,
            (iso_now(), claim_id),
        )

    def mark_status(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: int,
        status: str,
    ) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE vpn_trial_claims
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, iso_now(), claim_id),
        )
        claim = self.get_by_id(conn, claim_id)
        if claim is None:
            raise RuntimeError("Could not update VPN trial claim")
        return claim

    @staticmethod
    def _validate_trial_subscription(
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
    ) -> None:
        subscription = conn.execute(
            """
            SELECT user_id, kind
            FROM vpn_subscriptions
            WHERE id = ?
            """,
            (subscription_id,),
        ).fetchone()
        if subscription is None:
            raise ValueError("VPN trial subscription does not exist")
        if int(subscription["user_id"]) != user_id:
            raise ValueError("VPN trial subscription belongs to another user")
        if subscription["kind"] != "trial":
            raise ValueError("VPN trial claim requires a trial subscription")
