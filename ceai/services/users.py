from __future__ import annotations

from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.sessions import BotSessionRepository
from ceai.repositories.users import UserRepository


class UserService:
    def __init__(self, db: Database, vpn_db: Database | None = None) -> None:
        self.db = db
        self.vpn_db = vpn_db
        self.users = UserRepository()
        self.sessions = BotSessionRepository()

    def ensure_telegram_user(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
    ) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            user = self.users.upsert_telegram_user(
                conn,
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
            )
            if self.sessions.get_for_user(conn, user["id"]) is None:
                self.sessions.set_state(conn, user_id=user["id"], state="idle")

        if self.vpn_db is not None and self.vpn_db != self.db:
            with self.vpn_db.transaction() as vpn_conn:
                self.users.upsert_telegram_user(
                    vpn_conn,
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    language_code=language_code,
                    explicit_id=int(user["id"]),
                )
        return user

    def get_session(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.sessions.get_for_user(conn, user_id)

    def get_by_id(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.users.get_by_id(conn, user_id)

    def get_by_telegram_id(self, telegram_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.users.get_by_telegram_id(conn, telegram_id)

    def count_invited_users(self, user_id: int) -> int:
        with self.db.transaction() as conn:
            return self.users.count_invited_users(conn, user_id)

    def set_session(
        self, user_id: int, *, state: str, payload: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            return self.sessions.set_state(
                conn, user_id=user_id, state=state, payload=payload or {}
            )

    def clear_session(self, user_id: int) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            return self.sessions.clear(conn, user_id)
