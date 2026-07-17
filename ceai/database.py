from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ceai.config import BASE_DIR


class DatabaseConnection:
    def __init__(self, raw_conn: Any, *, driver: str) -> None:
        self.raw_conn = raw_conn
        self.driver = driver

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        if self.driver == "postgres":
            query = query.replace("?", "%s")
        else:
            query = query.replace("::jsonb", "")
        return self.raw_conn.execute(query, params)


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.driver = self._driver(database_url)
        if self.driver == "sqlite":
            self.path = self._sqlite_path(database_url)
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError(
                    "Postgres DATABASE_URL requires psycopg. Run `pip install -e .`."
                ) from exc

            url = database_url
            if url.startswith("postgres://"):
                url = "postgresql://" + url.removeprefix("postgres://")
            self.path = ""
            self._conn = psycopg.connect(url, row_factory=dict_row)
        self._lock = threading.RLock()

    @staticmethod
    def _driver(database_url: str) -> str:
        if database_url.startswith("sqlite:///"):
            return "sqlite"
        if database_url.startswith("postgresql://") or database_url.startswith(
            "postgres://"
        ):
            return "postgres"
        raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")

    @staticmethod
    def _sqlite_path(database_url: str) -> str:
        if database_url == "sqlite:///:memory:":
            return ":memory:"
        raw_path = database_url.replace("sqlite:///", "", 1)
        path = Path(raw_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return str(path)

    @property
    def conn(self) -> DatabaseConnection:
        return DatabaseConnection(self._conn, driver=self.driver)

    def close(self) -> None:
        self._conn.close()

    def migrate(self, migrations_dir: Path | None = None) -> None:
        if migrations_dir is not None:
            directory = migrations_dir
        elif self.driver == "postgres":
            directory = BASE_DIR / "migrations" / "postgres"
        else:
            directory = BASE_DIR / "migrations"

        with self._lock:
            self._ensure_schema_migrations()

        for migration in sorted(directory.glob("*.sql")):
            with self._lock:
                version = migration.name
                if self._migration_applied(version):
                    # psycopg starts a transaction even for this SELECT. Do
                    # not leave it open when every migration is already
                    # applied (the common production startup path).
                    if self.driver == "postgres":
                        self._conn.commit()
                    continue
                sql = migration.read_text(encoding="utf-8")
                if self.driver == "sqlite":
                    self._conn.executescript(sql)
                else:
                    for statement in sql.split(";"):
                        statement = statement.strip()
                        if statement:
                            self._conn.execute(statement)
                self._record_migration(version)
                self._conn.commit()

    def _execute_raw(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        if self.driver == "postgres":
            query = query.replace("?", "%s")
        return self._conn.execute(query, params)

    def _ensure_schema_migrations(self) -> None:
        if self.driver == "postgres":
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        else:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        self._conn.commit()

    def _migration_applied(self, version: str) -> bool:
        row = self._execute_raw(
            "SELECT 1 AS applied FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        return row is not None

    def _record_migration(self, version: str) -> None:
        self._execute_raw(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            (version,),
        )

    @contextmanager
    def transaction(self) -> Iterator[DatabaseConnection]:
        with self._lock:
            try:
                # psycopg with autocommit disabled starts a transaction before
                # the first statement automatically. Sending an explicit
                # BEGIN through execute() therefore creates a nested BEGIN and
                # floods Postgres with "transaction already in progress"
                # warnings. sqlite still needs the explicit boundary here.
                if self.driver == "sqlite":
                    self._conn.execute("BEGIN")
                yield DatabaseConnection(self._conn, driver=self.driver)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
