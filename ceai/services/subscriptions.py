from __future__ import annotations

from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.coins import CoinTransactionRepository
from ceai.repositories.plans import PlanRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.services.exceptions import NotFoundError
from ceai.services.subscription_recovery import (
    recover_active_subscription_from_paid_payment,
)


class SubscriptionService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.subscriptions = SubscriptionRepository()
        self.coins = CoinTransactionRepository()
        self.plans = PlanRepository()

    def active_for_user(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            subscription = self.subscriptions.get_active_for_user(conn, user_id)
            if subscription is None:
                subscription = recover_active_subscription_from_paid_payment(
                    conn, user_id=user_id, subscriptions=self.subscriptions
                )
            if subscription:
                balance = self.coins.balance_for_subscription(conn, subscription["id"])
                self.subscriptions.set_balance_cache(
                    conn, subscription_id=subscription["id"], balance=balance
                )
                subscription["coins_balance_cache"] = balance
            return subscription

    def balance_for_user(self, user_id: int) -> int:
        subscription = self.active_for_user(user_id)
        if not subscription:
            return 0
        return int(subscription["coins_balance_cache"])

    def disable_auto_renew(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.subscriptions.disable_auto_renew_for_user(
                conn,
                user_id=user_id,
            )

    def grant_channel_gift(
        self,
        user_id: int,
        *,
        plan_code: str,
        duration_days: int,
        coins_amount: int,
        gift_key: str,
    ) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            idempotency_key = f"gift:{gift_key}:{user_id}:credit"
            existing = self.coins.get_by_idempotency_key(conn, idempotency_key)
            if existing is not None:
                subscription = self.subscriptions.get_active_for_user(conn, user_id)
                if subscription:
                    balance = self.coins.balance_for_subscription(
                        conn, subscription["id"]
                    )
                    self.subscriptions.set_balance_cache(
                        conn, subscription_id=subscription["id"], balance=balance
                    )
                    subscription["coins_balance_cache"] = balance
                return {
                    "created": False,
                    "subscription": subscription,
                    "credited_coins": 0,
                }

            active_subscription = self.subscriptions.get_active_for_user(conn, user_id)
            if active_subscription:
                plan_id = active_subscription["plan_id"]
            else:
                plan = self.plans.get_by_code(conn, plan_code)
                if plan is None or not plan.get("is_active"):
                    raise NotFoundError("Тариф для подарка не найден.")
                plan_id = plan["id"]

            subscription = self.subscriptions.extend_or_create_active(
                conn,
                user_id=user_id,
                plan_id=plan_id,
                duration_days=duration_days,
            )
            _transaction, created = self.coins.create(
                conn,
                user_id=user_id,
                subscription_id=subscription["id"],
                amount=coins_amount,
                type_="credit",
                status="completed",
                reason="channel_gift",
                idempotency_key=idempotency_key,
            )
            balance = self.coins.balance_for_subscription(conn, subscription["id"])
            self.subscriptions.set_balance_cache(
                conn, subscription_id=subscription["id"], balance=balance
            )
            subscription = self.subscriptions.get_by_id(conn, subscription["id"])
            if subscription is None:
                raise RuntimeError("Could not load gift subscription")
            subscription["coins_balance_cache"] = balance
            return {
                "created": created,
                "subscription": subscription,
                "credited_coins": coins_amount if created else 0,
            }

    def has_channel_gift(self, user_id: int, *, gift_key: str) -> bool:
        idempotency_key = f"gift:{gift_key}:{user_id}:credit"
        with self.db.transaction() as conn:
            return self.coins.get_by_idempotency_key(conn, idempotency_key) is not None
