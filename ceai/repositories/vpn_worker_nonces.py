from __future__ import annotations

import sqlite3
from datetime import timedelta

from ceai.time_utils import iso_now, parse_iso


class VpnWorkerNonceRepository:
    """Persist one-time worker request nonces to prevent signed-request replay."""

    def consume(
        self,
        conn: sqlite3.Connection,
        *,
        worker_id: str,
        nonce: str,
        seen_at: str | None = None,
        ttl_seconds: int = 600,
    ) -> bool:
        if not worker_id or not nonce:
            return False
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")

        current = seen_at or iso_now()
        expires_at = (parse_iso(current) + timedelta(seconds=ttl_seconds)).isoformat()
        conn.execute(
            "DELETE FROM vpn_worker_nonces WHERE expires_at <= ?",
            (current,),
        )
        row = conn.execute(
            """
            INSERT INTO vpn_worker_nonces (worker_id, nonce, seen_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(worker_id, nonce) DO NOTHING
            RETURNING nonce
            """,
            (worker_id, nonce, current, expires_at),
        ).fetchone()
        return row is not None
