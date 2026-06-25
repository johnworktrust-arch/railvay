from __future__ import annotations

from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.coins import CoinTransactionRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.services.subscription_recovery import (
    recover_active_subscription_from_paid_payment,
)


class SubscriptionService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.subscriptions = SubscriptionRepository()
        self.coins = CoinTransactionRepository()

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
