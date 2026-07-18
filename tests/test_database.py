from __future__ import annotations

import threading
import unittest

from ceai.database import Database
from ceai.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query: str, params=()):
        self.statements.append((query, tuple(params)))
        return self

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def fetchone(self):
        return None


class DatabaseTransactionTest(unittest.TestCase):
    def _postgres_database(self) -> tuple[Database, _FakePostgresConnection]:
        raw = _FakePostgresConnection()
        database = Database.__new__(Database)
        database.driver = "postgres"
        database._conn = raw
        database._lock = threading.RLock()
        return database, raw

    def test_postgres_relies_on_psycopg_implicit_begin(self) -> None:
        database, raw = self._postgres_database()

        with database.transaction() as conn:
            conn.execute("SELECT ?", (7,))

        self.assertEqual(raw.statements, [("SELECT %s", (7,))])
        self.assertEqual(raw.commits, 1)
        self.assertEqual(raw.rollbacks, 0)

    def test_postgres_transaction_rolls_back_errors(self) -> None:
        database, raw = self._postgres_database()

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with database.transaction():
                raise RuntimeError("boom")

        self.assertEqual(raw.commits, 0)
        self.assertEqual(raw.rollbacks, 1)

    def test_postgres_vpn_claim_types_nullable_profile_prefix(self) -> None:
        database, raw = self._postgres_database()

        claimed = VpnProvisioningJobRepository().claim_due(
            database.conn,
            server_id=1,
            excluded_idempotency_prefix=None,
        )

        self.assertIsNone(claimed)
        query, params = raw.statements[-1]
        self.assertIn("CAST(%s AS TEXT) IS NULL", query)
        self.assertIsNone(params[5])
        self.assertIsNone(params[6])


if __name__ == "__main__":
    unittest.main()
