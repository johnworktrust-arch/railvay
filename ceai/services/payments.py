from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.payments import PaymentRepository
from ceai.repositories.plans import PlanRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.repositories.webhooks import WebhookLogRepository
from ceai.services.coins import CoinService
from ceai.services.exceptions import BusinessRuleError, NotFoundError
from ceai.services.referrals import ReferralService


YOOKASSA_PROVIDER = "yookassa"


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
        payment_provider: str = "mock",
        app_base_url: str = "",
        yookassa_shop_id: str = "",
        yookassa_secret_key: str = "",
        yookassa_api_base_url: str = "https://api.yookassa.ru/v3",
        yookassa_return_path: str = "/payments/yookassa/return",
        yookassa_request_timeout_seconds: int = 15,
        referrals: ReferralService | None = None,
    ) -> None:
        self.db = db
        self.mock_payment_base_url = mock_payment_base_url.rstrip("/")
        self.payment_provider = (payment_provider or "mock").strip().lower()
        self.app_base_url = app_base_url.rstrip("/")
        self.yookassa_shop_id = yookassa_shop_id.strip()
        self.yookassa_secret_key = yookassa_secret_key.strip()
        self.yookassa_api_base_url = yookassa_api_base_url.rstrip("/")
        self.yookassa_return_path = yookassa_return_path
        self.yookassa_request_timeout_seconds = yookassa_request_timeout_seconds
        self.plans = PlanRepository()
        self.payments = PaymentRepository()
        self.subscriptions = SubscriptionRepository()
        self.webhooks = WebhookLogRepository()
        self.coins = CoinService()
        self.referrals = referrals or ReferralService(db)

    def create_payment(
        self, *, user_id: int, plan_code: str, payment_method: str = ""
    ) -> Dict[str, Any]:
        if self.payment_provider == YOOKASSA_PROVIDER:
            return self.create_yookassa_payment(user_id=user_id, plan_code=plan_code)
        return self.create_mock_payment(
            user_id=user_id,
            plan_code=plan_code,
            payment_method=payment_method or self.payment_provider,
        )

    def create_mock_payment(
        self, *, user_id: int, plan_code: str, payment_method: str = "mock"
    ) -> Dict[str, Any]:
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
                provider="mock",
                meta={
                    "kind": "mock_payment",
                    "payment_method": payment_method or "mock",
                },
            )

    def create_yookassa_payment(
        self, *, user_id: int, plan_code: str
    ) -> Dict[str, Any]:
        if not self.yookassa_shop_id or not self.yookassa_secret_key:
            raise BusinessRuleError(
                "ЮKassa не настроена: нужны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY."
            )
        if not self.app_base_url:
            raise BusinessRuleError(
                "ЮKassa не настроена: нужен APP_BASE_URL с публичным адресом бота."
            )

        with self.db.transaction() as conn:
            plan = self.plans.get_by_code(conn, plan_code)
            if plan is None or not plan["is_active"]:
                raise NotFoundError("Тариф не найден")

        idempotence_key = f"ceaai-payment-{uuid.uuid4().hex}"
        return_url = self._public_url(self.yookassa_return_path)
        amount_rub = int(plan["price_rub"])
        payload = {
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB",
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "description": self._payment_description(plan),
            "metadata": {
                "kind": "subscription",
                "user_id": str(user_id),
                "plan_id": str(plan["id"]),
                "plan_code": str(plan["code"]),
                "idempotence_key": idempotence_key,
            },
        }
        response = self._yookassa_request(
            "POST",
            "/payments",
            payload=payload,
            idempotence_key=idempotence_key,
        )
        external_id = str(response.get("id") or "")
        confirmation = response.get("confirmation")
        payment_url = (
            str(confirmation.get("confirmation_url") or "")
            if isinstance(confirmation, dict)
            else ""
        )
        if not external_id or not payment_url:
            raise BusinessRuleError("ЮKassa не вернула ссылку на оплату.")

        with self.db.transaction() as conn:
            return self.payments.create_pending(
                conn,
                user_id=user_id,
                plan_id=plan["id"],
                amount_rub=amount_rub,
                external_id=external_id,
                payment_url=payment_url,
                provider=YOOKASSA_PROVIDER,
                meta={
                    "kind": "yookassa_payment",
                    "idempotence_key": idempotence_key,
                    "return_url": return_url,
                    "response": response,
                },
            )

    def process_mock_success_webhook(
        self, *, external_id: str
    ) -> PaymentWebhookResult:
        provider = "mock"
        event_type = "payment.succeeded"
        payload = {"provider": provider, "external_id": external_id, "status": "paid"}
        webhook, should_process, duplicate = self._reserve_webhook(
            provider=provider,
            external_id=external_id,
            event_type=event_type,
            payload=payload,
        )
        if not should_process:
            return PaymentWebhookResult(
                processed=False,
                duplicate=duplicate,
                payment=None,
                subscription=None,
                credited_coins=0,
                message="Webhook already processed or received",
            )
        return self._process_successful_payment(
            provider=provider,
            external_id=external_id,
            webhook_id=int(webhook["id"]),
            payload=payload,
        )

    def process_yookassa_webhook(
        self, *, payload: Dict[str, Any]
    ) -> PaymentWebhookResult:
        event_type = str(payload.get("event") or "")
        payment_object = payload.get("object")
        if not isinstance(payment_object, dict):
            raise BusinessRuleError("Некорректный webhook ЮKassa: нет object.")
        external_id = str(payment_object.get("id") or "")
        if not external_id:
            raise BusinessRuleError("Некорректный webhook ЮKassa: нет payment id.")

        webhook, should_process, duplicate = self._reserve_webhook(
            provider=YOOKASSA_PROVIDER,
            external_id=external_id,
            event_type=event_type,
            payload=payload,
        )
        if not should_process:
            return PaymentWebhookResult(
                processed=False,
                duplicate=duplicate,
                payment=None,
                subscription=None,
                credited_coins=0,
                message="Webhook already processed or received",
            )

        if event_type != "payment.succeeded":
            self._mark_webhook(
                webhook_id=int(webhook["id"]),
                status="ignored",
                error_message=f"Unsupported event {event_type}",
            )
            return PaymentWebhookResult(
                processed=False,
                duplicate=False,
                payment=None,
                subscription=None,
                credited_coins=0,
                message=f"Webhook ignored: {event_type}",
            )

        try:
            verified_payment = self._fetch_yookassa_payment(external_id)
        except Exception as exc:
            self._mark_webhook(
                webhook_id=int(webhook["id"]),
                status="failed",
                error_message=str(exc),
            )
            raise

        if not self._is_yookassa_payment_succeeded(verified_payment):
            self._mark_webhook(
                webhook_id=int(webhook["id"]),
                status="ignored",
                error_message="YooKassa payment is not succeeded",
            )
            return PaymentWebhookResult(
                processed=False,
                duplicate=False,
                payment=None,
                subscription=None,
                credited_coins=0,
                message="YooKassa payment is not succeeded",
            )

        return self._process_successful_payment(
            provider=YOOKASSA_PROVIDER,
            external_id=external_id,
            webhook_id=int(webhook["id"]),
            payload={
                "webhook": payload,
                "verified_payment": verified_payment,
            },
        )

    def _reserve_webhook(
        self,
        *,
        provider: str,
        external_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool, bool]:
        with self.db.transaction() as conn:
            webhook, created = self.webhooks.create_received(
                conn,
                provider=provider,
                external_id=external_id,
                event_type=event_type,
                payload=payload,
            )
            if created or webhook.get("status") == "failed":
                return webhook, True, False
            return webhook, False, True

    def _process_successful_payment(
        self,
        *,
        provider: str,
        external_id: str,
        webhook_id: int,
        payload: Dict[str, Any],
    ) -> PaymentWebhookResult:
        with self.db.transaction() as conn:
            payment = self.payments.get_by_external_id(conn, provider, external_id)
            if payment is None:
                self.webhooks.mark(
                    conn,
                    webhook_id=webhook_id,
                    status="failed",
                    error_message="Payment not found",
                )
                raise NotFoundError("Платеж не найден")

            if payment["status"] == "paid":
                self.webhooks.mark(conn, webhook_id=webhook_id, status="ignored")
                return PaymentWebhookResult(
                    processed=False,
                    duplicate=False,
                    payment=payment,
                    subscription=None,
                    credited_coins=0,
                    message="Payment already paid",
                )

            if payment["status"] != "pending":
                self.webhooks.mark(conn, webhook_id=webhook_id, status="ignored")
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
                    webhook_id=webhook_id,
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
                provider=provider,
            )
            referral_credit = self.referrals.credit_for_payment_in_transaction(
                conn,
                payment=payment,
            )
            balance = self.coins.sync_subscription_cache(conn, subscription["id"])
            subscription = self.subscriptions.get_by_id(conn, subscription["id"])
            if subscription is not None:
                subscription["coins_balance_cache"] = balance
            self.webhooks.mark(conn, webhook_id=webhook_id, status="processed")

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

    def _mark_webhook(
        self,
        *,
        webhook_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            self.webhooks.mark(
                conn,
                webhook_id=webhook_id,
                status=status,
                error_message=error_message,
            )

    def _fetch_yookassa_payment(self, payment_id: str) -> Dict[str, Any]:
        return self._yookassa_request("GET", f"/payments/{payment_id}")

    def _yookassa_request(
        self,
        method: str,
        path: str,
        *,
        payload: Dict[str, Any] | None = None,
        idempotence_key: str | None = None,
    ) -> Dict[str, Any]:
        if not self.yookassa_shop_id or not self.yookassa_secret_key:
            raise BusinessRuleError("ЮKassa не настроена.")

        url = self.yookassa_api_base_url + "/" + path.lstrip("/")
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._yookassa_auth_header(),
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.yookassa_request_timeout_seconds
            ) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise BusinessRuleError(
                f"ЮKassa API error {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BusinessRuleError(f"ЮKassa API недоступна: {exc.reason}") from exc

        data = json.loads(response_body) if response_body else {}
        if not isinstance(data, dict):
            raise BusinessRuleError("ЮKassa вернула некорректный ответ.")
        return data

    def _yookassa_auth_header(self) -> str:
        raw = f"{self.yookassa_shop_id}:{self.yookassa_secret_key}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _public_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.app_base_url}{normalized_path}"

    @staticmethod
    def _payment_description(plan: Dict[str, Any]) -> str:
        description = f"CeaAI: тариф {plan['name']} на {plan['duration_days']} дней"
        return description[:128]

    @staticmethod
    def _is_yookassa_payment_succeeded(payment: Dict[str, Any]) -> bool:
        return payment.get("status") == "succeeded" and bool(payment.get("paid"))
