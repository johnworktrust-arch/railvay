from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from ceai.repositories.base import row_to_dict, rows_to_dicts
from ceai.time_utils import iso_now, parse_iso


class VpnProvisioningJobRepository:
    def enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        subscription_id: int,
        operation: str,
        idempotency_key: str,
        next_attempt_at: str | None = None,
    ) -> Tuple[Dict[str, Any], bool]:
        now = iso_now()
        cursor = conn.execute(
            """
            INSERT INTO vpn_provisioning_jobs (
                subscription_id, operation, status, attempts,
                next_attempt_at, idempotency_key, created_at, updated_at
            )
            VALUES (?, ?, 'pending', 0, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                subscription_id,
                operation,
                next_attempt_at or now,
                idempotency_key,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            job = self.get_by_id(conn, int(row["id"]))
            if job is None:
                raise RuntimeError("Could not enqueue VPN provisioning job")
            return job, True

        job = self.get_by_idempotency_key(conn, idempotency_key)
        if job is None:
            raise RuntimeError("Could not load existing VPN provisioning job")
        return job, False

    def get_by_id(
        self, conn: sqlite3.Connection, job_id: int
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vpn_provisioning_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )

    def get_by_idempotency_key(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_provisioning_jobs
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        )

    def list_due(
        self,
        conn: sqlite3.Connection,
        *,
        due_at: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        current = due_at or iso_now()
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM vpn_provisioning_jobs
                WHERE (
                    status IN ('pending', 'failed')
                    AND next_attempt_at <= ?
                ) OR (
                    status = 'running'
                    AND lease_expires_at <= ?
                )
                ORDER BY
                    CASE
                        WHEN status = 'running' THEN lease_expires_at
                        ELSE next_attempt_at
                    END ASC,
                    id ASC
                LIMIT ?
                """,
                (current, current, limit),
            ).fetchall()
        )

    def claim_due(
        self,
        conn: sqlite3.Connection,
        *,
        due_at: str | None = None,
        lease_seconds: int = 60,
        lease_token: str | None = None,
        server_id: int | None = None,
    ) -> Dict[str, Any] | None:
        """Atomically claim one due job, including a job with an expired lease."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if lease_token is not None and not lease_token:
            raise ValueError("lease_token must not be empty")

        current = due_at or iso_now()
        lease_expires_at = (parse_iso(current) + timedelta(seconds=lease_seconds)).isoformat()
        token = lease_token or secrets.token_urlsafe(24)
        cursor = conn.execute(
            """
            UPDATE vpn_provisioning_jobs
            SET status = 'running', attempts = attempts + 1,
                last_error = NULL, lease_token = ?, lease_expires_at = ?,
                updated_at = ?, completed_at = NULL
            WHERE id = (
                SELECT id
                FROM vpn_provisioning_jobs
                WHERE (
                    (status IN ('pending', 'failed') AND next_attempt_at <= ?)
                    OR (status = 'running' AND lease_expires_at <= ?)
                )
                  AND (
                      ? IS NULL OR subscription_id IN (
                          SELECT id FROM vpn_subscriptions WHERE server_id = ?
                      )
                  )
                ORDER BY
                    CASE
                        WHEN status = 'running' THEN lease_expires_at
                        ELSE next_attempt_at
                    END ASC,
                    id ASC
                LIMIT 1
            )
              AND (
                  (status IN ('pending', 'failed') AND next_attempt_at <= ?)
                  OR (status = 'running' AND lease_expires_at <= ?)
              )
              AND (
                  ? IS NULL OR subscription_id IN (
                      SELECT id FROM vpn_subscriptions WHERE server_id = ?
                  )
              )
            RETURNING id
            """,
            (
                token,
                lease_expires_at,
                current,
                current,
                current,
                server_id,
                server_id,
                current,
                current,
                server_id,
                server_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._require_by_id(conn, int(row["id"]), "claim")

    def mark_completed(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: int,
        lease_token: str,
    ) -> Dict[str, Any]:
        now = iso_now()
        cursor = conn.execute(
            """
            UPDATE vpn_provisioning_jobs
            SET status = 'completed', last_error = NULL,
                lease_token = NULL, lease_expires_at = NULL,
                updated_at = ?, completed_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id
            """,
            (now, now, job_id, lease_token),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("Could not complete VPN provisioning job: lease lost")
        return self._require_by_id(conn, job_id, "complete")

    def mark_failed(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: int,
        lease_token: str,
        error_message: str,
        next_attempt_at: str,
    ) -> Dict[str, Any]:
        cursor = conn.execute(
            """
            UPDATE vpn_provisioning_jobs
            SET status = 'failed', last_error = ?, next_attempt_at = ?,
                lease_token = NULL, lease_expires_at = NULL,
                updated_at = ?, completed_at = NULL
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id
            """,
            (
                error_message,
                next_attempt_at,
                iso_now(),
                job_id,
                lease_token,
            ),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("Could not fail VPN provisioning job: lease lost")
        return self._require_by_id(conn, job_id, "fail")

    def _require_by_id(
        self, conn: sqlite3.Connection, job_id: int, action: str
    ) -> Dict[str, Any]:
        job = self.get_by_id(conn, job_id)
        if job is None:
            raise RuntimeError(f"Could not {action} VPN provisioning job")
        return job
