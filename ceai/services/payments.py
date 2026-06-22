from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.payments import PaymentRepository
from ceai.repositories.plans import PlanRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.repositories.webhooks import WebhookLogRepository
from ceai.services.coins import CoinService
from ceai.services.exceptions import NotFoundError
from ceai.services.referrals import ReferralService


@dataclass(frozen=True)
class PaymentWebhookResult:
    processed: bool
    duplicate: bool
    payment: Dict[str, Any] | None
    subscription: Dict[str, Any] | None
    credited_coins: int
    message: str
    referral_reward_kopecks: int = 0


class PaymentService:
    def __init__(
        self,
        db: Database,
        *,
        mock_payment_base_url: str,
        referrals: ReferralService | None = None,
    ) -> None:
        self.db = db
        self.mock_payment_base_url = mock_payment_base_url.rstrip("/")
        self.plans = PlanRepository()
        self.payments = PaymentRepository()
        self.subscriptions = SubscriptionRepository()
        self.webhooks = WebhookLogRepository()
        self.coins = CoinService()
        self.referrals = referrals or ReferralService(db)

    def create_mock_payment(self, *, user_id: int, plan_code: str) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            plan = self.plans.get_by_code(conn, plan_code)
            if plan is None or not plan["is_active"]:
                raise NotFoundError("Тариф не найден")
            external_id = f"mock_{uuid.uuid4().hex}"
            payment_url = f"{self.mock_payment_base_url}/{external_id}"
            return self.payments.create_pending(
                conn,
                user_id=user_id,
                plan_id=plan["id"],
                amount_rub=plan["price_rub"],
                external_id=external_id,
                payment_url=payment_url,
            )

    def process_mock_success_webhook(
        self, *, external_id: str
    ) -> PaymentWebhookResult:
        provider = "mock"
        event_type = "payment.succeeded"
        payload = {"provider": provider, "external_id": external_id, "status": "paid"}

        with self.db.transaction() as conn:
            webhook, created = self.webhooks.create_received(
                conn,
                provider=provider,
                external_id=external_id,
                event_type=event_type,
                payload=payload,
            )
            if not created:
                return PaymentWebhookResult(
                    processed=False,
                    duplicate=True,
                    payment=None,
                    subscription=None,
                    credited_coins=0,
                    message="Webhook already processed or received",
                )

            payment = self.payments.get_by_external_id(conn, provider, external_id)
            if payment is None:
                self.webhooks.mark(
                    conn,
                    webhook_id=webhook["id"],
                    status="failed",
                    error_message="Payment not found",
                )
                raise NotFoundError("Платеж не найден")

            if payment["status"] == "paid":
                self.webhooks.mark(conn, webhook_id=webhook["id"], status="ignored")
                return PaymentWebhookResult(
                    processed=False,
                    duplicate=False,
                    payment=payment,
                    subscription=None,
                    credited_coins=0,
                    message="Payment already paid",
                )

            if payment["status"] != "pending":
                self.webhooks.mark(conn, webhook_id=webhook["id"], status="ignored")
                return PaymentWebhookResult(
                    processed=False,
                    duplicate=False,
                    payment=payment,
                    subscription=None,
                    credited_coins=0,
                    message=f"Payment status is {payment['status']}",
                )

            plan = self.plans.get_by_id(conn, payment["plan_id"])
            if plan is None:
                self.webhooks.mark(
                    conn,
                    webhook_id=webhook["id"],
                    status="failed",
                    error_message="Plan not found",
                )
                raise NotFoundError("Тариф платежа не найден")

            subscription = self.subscriptions.extend_or_create_active(
                conn,
                user_id=payment["user_id"],
                plan_id=plan["id"],
                duration_days=plan["duration_days"],
            )
            payment = self.payments.mark_paid(
                conn,
                payment_id=payment["id"],
                subscription_id=subscription["id"],
                meta=payload,
            )
            self.coins.credit_payment(
                conn,
                user_id=payment["user_id"],
                subscription_id=subscription["id"],
                payment_id=payment["id"],
                amount=plan["coins_amount"],
                external_id=external_id,
            )
            referral_credit = self.referrals.credit_for_payment_in_transaction(
                conn,
                payment=payment,
            )
            balance = self.coins.sync_subscription_cache(conn, subscription["id"])
            subscription = self.subscriptions.get_by_id(conn, subscription["id"])
            if subscription is not None:
                subscription["coins_balance_cache"] = balance
            self.webhooks.mark(conn, webhook_id=webhook["id"], status="processed")

            return PaymentWebhookResult(
                processed=True,
                duplicate=False,
                payment=payment,
                subscription=subscription,
                credited_coins=plan["coins_amount"],
                message="Payment processed",
                referral_reward_kopecks=referral_credit.amount_kopecks,
            )

    def process_mock_success_webhook_for_payment_id(
        self, *, payment_id: int
    ) -> PaymentWebhookResult:
        with self.db.transaction() as conn:
            payment = self.payments.get_by_id(conn, payment_id)
            if payment is None:
                raise NotFoundError("Платеж не найден")
            external_id = payment["external_id"]
        return self.process_mock_success_webhook(external_id=external_id)
