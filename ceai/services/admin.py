from __future__ import annotations

import math
import uuid
from typing import Any, Dict

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.admin import AdminRepository
from ceai.repositories.app_settings import AppSettingsRepository
from ceai.repositories.coins import CoinTransactionRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.repositories.users import UserRepository
from ceai.services.coins import CoinService
from ceai.services.exceptions import BusinessRuleError, NotFoundError


ADMIN_PAGE_SIZE = 10
MAINTENANCE_MODE_KEY = "MAINTENANCE_MODE"


class AdminService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.admins = AdminRepository()
        self.app_settings = AppSettingsRepository()
        self.users = UserRepository()
        self.subscriptions = SubscriptionRepository()
        self.coin_transactions = CoinTransactionRepository()
        self.coins = CoinService()

    def _is_bootstrap_owner(self, user: Dict[str, Any]) -> bool:
        telegram_id = int(user["telegram_id"])
        username = (user.get("username") or "").lower()
        return (
            telegram_id in self.settings.admin_telegram_ids
            or username in self.settings.admin_telegram_usernames
        )

    def ensure_admin_access(self, user: Dict[str, Any]) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            if self._is_bootstrap_owner(user):
                return self.admins.upsert_admin(
                    conn, user_id=user["id"], role="owner", is_active=True
                )
            admin = self.admins.get_admin_by_user_id(conn, user["id"])
            if admin and admin["is_active"] and admin["role"] in {
                "owner",
                "admin",
                "support",
            }:
                return admin
            return None

    def has_admin_access(self, user: Dict[str, Any]) -> bool:
        return self.ensure_admin_access(user) is not None

    def is_blocked_regular_user(self, user: Dict[str, Any]) -> bool:
        return bool(user.get("is_blocked")) and not self.has_admin_access(user)

    def is_restricted_regular_user(self, user: Dict[str, Any]) -> bool:
        if self.has_admin_access(user):
            return False
        return bool(user.get("is_blocked")) or self.is_maintenance_mode_active()

    def is_maintenance_mode_active(self) -> bool:
        with self.db.transaction() as conn:
            values = self.app_settings.get_many(conn, [MAINTENANCE_MODE_KEY])
        return values.get(MAINTENANCE_MODE_KEY) == "1"

    def set_maintenance_mode(self, *, admin: Dict[str, Any], is_active: bool) -> bool:
        if not self.can_manage(admin):
            raise BusinessRuleError("Недостаточно прав")
        with self.db.transaction() as conn:
            self.app_settings.upsert(
                conn,
                key=MAINTENANCE_MODE_KEY,
                value="1" if is_active else "0",
                is_secret=False,
            )
            self.admins.log_action(
                conn,
                admin_user_id=admin["user_id"],
                target_user_id=None,
                action="maintenance_on" if is_active else "maintenance_off",
                payload={"is_active": is_active},
            )
        return is_active

    def toggle_maintenance_mode(self, *, admin: Dict[str, Any]) -> bool:
        return self.set_maintenance_mode(
            admin=admin,
            is_active=not self.is_maintenance_mode_active(),
        )

    def can_manage(self, admin: Dict[str, Any]) -> bool:
        return bool(admin and admin["is_active"] and admin["role"] in {"owner", "admin"})

    def stats(self) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            return self.admins.stats(conn)

    def list_users(self, *, page: int, page_size: int = ADMIN_PAGE_SIZE) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            total = self.admins.count_users(conn)
            pages = max(math.ceil(total / page_size), 1)
            page = min(max(page, 1), pages)
            return {
                "users": self.admins.list_users(
                    conn, page=page, page_size=page_size
                ),
                "page": page,
                "pages": pages,
                "total": total,
            }

    def find_user(self, query: str) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.admins.find_user(conn, query)

    def user_card(self, user_id: int) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            card = self.admins.user_card(conn, user_id)
            if card is None:
                raise NotFoundError("Пользователь не найден")
            return card

    def set_blocked(
        self, *, admin: Dict[str, Any], target_user_id: int, is_blocked: bool
    ) -> None:
        if not self.can_manage(admin):
            raise BusinessRuleError("Недостаточно прав")
        with self.db.transaction() as conn:
            target = self.users.get_by_id(conn, target_user_id)
            if target is None:
                raise NotFoundError("Пользователь не найден")
            self.admins.set_blocked(
                conn, user_id=target_user_id, is_blocked=is_blocked
            )
            self.admins.log_action(
                conn,
                admin_user_id=admin["user_id"],
                target_user_id=target_user_id,
                action="ban" if is_blocked else "unban",
                payload={"is_blocked": is_blocked},
            )

    def manual_credit(
        self, *, admin: Dict[str, Any], target_user_id: int, amount: int
    ) -> int:
        if not self.can_manage(admin):
            raise BusinessRuleError("Недостаточно прав")
        if amount <= 0:
            raise BusinessRuleError("Введите положительное целое число")

        with self.db.transaction() as conn:
            target = self.users.get_by_id(conn, target_user_id)
            if target is None:
                raise NotFoundError("Пользователь не найден")
            subscription = self.subscriptions.get_active_for_user(conn, target_user_id)
            if subscription is None:
                raise BusinessRuleError("У пользователя нет активной подписки")

            transaction, created = self.coin_transactions.create(
                conn,
                user_id=target_user_id,
                subscription_id=subscription["id"],
                amount=amount,
                type_="manual_adjustment",
                status="completed",
                reason="admin_manual_credit",
                idempotency_key=(
                    f"admin:{admin['user_id']}:credit:"
                    f"{target_user_id}:{amount}:{subscription['id']}:{uuid.uuid4().hex}"
                ),
            )
            balance = self.coins.sync_subscription_cache(conn, subscription["id"])
            self.admins.log_action(
                conn,
                admin_user_id=admin["user_id"],
                target_user_id=target_user_id,
                action="manual_credit",
                payload={
                    "amount": amount,
                    "subscription_id": subscription["id"],
                    "transaction_id": transaction["id"],
                    "created": created,
                },
            )
            return balance
