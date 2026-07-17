from __future__ import annotations

import threading
import unittest

from ceai.database import Database


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


if __name__ == "__main__":
    unittest.main()
