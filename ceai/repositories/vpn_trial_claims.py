from __future__ import annotations

import sqlite3
from typing import Any, Dict, Tuple

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
