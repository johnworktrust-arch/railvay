from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

from ceai.repositories.payments import PaymentRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.services.coins import CoinService


def recover_active_subscription_from_paid_payment(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    subscriptions: SubscriptionRepository | None = None,
    payments: PaymentRepository | None = None,
    coins: CoinService | None = None,
) -> Dict[str, Any] | None:
    subscription_repo = subscriptions or SubscriptionRepository()
    payment_repo = payments or PaymentRepository()
    coin_service = coins or CoinService()

    payment = payment_repo.latest_paid_with_plan_for_user(conn, user_id)
    if payment is None:
        return None

    paid_at = _parse_datetime(payment.get("paid_at") or payment.get("created_at"))
    if paid_at is None:
        return None

    plan_coins_amount = int(payment["plan_coins_amount"])
    subscription = subscription_repo.restore_paid_period(
        conn,
        user_id=user_id,
        plan_id=int(payment["plan_id"]),
        starts_at=paid_at,
        duration_days=int(payment["plan_duration_days"]),
        preferred_subscription_id=(
            int(payment["subscription_id"]) if payment.get("subscription_id") else None
        ),
    )
    if subscription is None:
        return None

    if int(payment.get("subscription_id") or 0) != int(subscription["id"]):
        payment = payment_repo.set_subscription_id(
            conn, payment_id=int(payment["id"]), subscription_id=int(subscription["id"])
        )

    coin_service.credit_payment(
        conn,
        user_id=user_id,
        subscription_id=int(subscription["id"]),
        payment_id=int(payment["id"]),
        amount=plan_coins_amount,
        external_id=str(payment["external_id"]),
        provider=str(payment["provider"]),
    )
    balance = coin_service.sync_subscription_cache(conn, int(subscription["id"]))
    subscription["coins_balance_cache"] = balance
    return subscription


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
