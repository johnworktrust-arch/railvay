from __future__ import annotations

import sqlite3
from typing import Any, Dict

from ceai.repositories.coins import CoinTransactionRepository
from ceai.repositories.subscriptions import SubscriptionRepository


class CoinService:
    def __init__(self) -> None:
        self.transactions = CoinTransactionRepository()
        self.subscriptions = SubscriptionRepository()

    def balance_for_subscription(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> int:
        return self.transactions.balance_for_subscription(conn, subscription_id)

    def sync_subscription_cache(
        self, conn: sqlite3.Connection, subscription_id: int
    ) -> int:
        balance = self.balance_for_subscription(conn, subscription_id)
        self.subscriptions.set_balance_cache(
            conn, subscription_id=subscription_id, balance=balance
        )
        return balance

    def credit_payment(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
        payment_id: int,
        amount: int,
        external_id: str,
    ) -> Dict[str, Any]:
        transaction, _ = self.transactions.create(
            conn,
            user_id=user_id,
            subscription_id=subscription_id,
            payment_id=payment_id,
            amount=amount,
            type_="credit",
            status="completed",
            reason="subscription_grant",
            idempotency_key=f"payment:mock:{external_id}:credit",
        )
        self.sync_subscription_cache(conn, subscription_id)
        return transaction

    def reserve_generation(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
        generation_id: int,
        amount: int,
    ) -> Dict[str, Any]:
        transaction, _ = self.transactions.create(
            conn,
            user_id=user_id,
            subscription_id=subscription_id,
            generation_id=generation_id,
            amount=-amount,
            type_="reserve",
            status="completed",
            reason="generation_reserve",
            idempotency_key=f"generation:{generation_id}:reserve",
        )
        self.sync_subscription_cache(conn, subscription_id)
        return transaction

    def finalize_generation_charge(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
        generation_id: int,
    ) -> Dict[str, Any]:
        transaction, _ = self.transactions.create(
            conn,
            user_id=user_id,
            subscription_id=subscription_id,
            generation_id=generation_id,
            amount=0,
            type_="debit",
            status="completed",
            reason="generation_charge_finalized",
            idempotency_key=f"generation:{generation_id}:charge",
        )
        self.sync_subscription_cache(conn, subscription_id)
        return transaction

    def refund_generation(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        subscription_id: int,
        generation_id: int,
        amount: int,
    ) -> Dict[str, Any]:
        transaction, _ = self.transactions.create(
            conn,
            user_id=user_id,
            subscription_id=subscription_id,
            generation_id=generation_id,
            amount=amount,
            type_="refund",
            status="completed",
            reason="generation_failed_refund",
            idempotency_key=f"generation:{generation_id}:refund",
        )
        self.sync_subscription_cache(conn, subscription_id)
        return transaction
