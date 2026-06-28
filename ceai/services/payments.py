from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

from ceai.database import Database
from ceai.json_utils import loads_dict
from ceai.pricing import telegram_stars_amount_for_rub
from ceai.repositories.payments import PaymentRepository
from ceai.repositories.plans import PlanRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.repositories.webhooks import WebhookLogRepository
from ceai.services.coins import CoinService
from ceai.services.exceptions import BusinessRuleError, NotFoundError
from ceai.services.referrals import ReferralService
from ceai.time_utils import utcnow


YOOKASSA_PROVIDER = "yookassa"
YOOKASSA_METHODS = {"yookassa", "card", "cards", "card_sbp", "sbp"}
YOOKASSA_MIN_TIMEOUT_SECONDS = 30
YOOKASSA_NETWORK_ATTEMPTS = 3
YOOKASSA_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    ssl.SSLError,
)
AUTO_RENEWAL_LOOKAHEAD_SECONDS = 3600
AUTO_RENEWAL_RETRY_INTERVAL_SECONDS = 6 * 3600
AUTO_RENEWAL_BATCH_SIZE = 25
CRYPTO_PAY_PROVIDER = "crypto_pay"
CRYPTO_PAY_METHODS = {"crypto", "crypto_pay", "usdt_trc20"}
TELEGRAM_STARS_PROVIDER = "telegram_stars"
TELEGRAM_STARS_METHODS = {"telegram_stars", "stars"}


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
        crypto_pay_token: str = "",
        crypto_pay_api_base_url: str = "https://testnet-pay.crypt.bot/api",
        crypto_pay_webhook_secret: str = "",
        crypto_pay_accepted_assets: str = "USDT",
        crypto_pay_request_timeout_seconds: int = 15,
        telegram_stars_amount: int = 0,
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
        self.yookassa_request_timeout_seconds = max(
            YOOKASSA_MIN_TIMEOUT_SECONDS,
            yookassa_request_timeout_seconds,
        )
        self.crypto_pay_token = crypto_pay_token.strip()
        self.crypto_pay_api_base_url = self._normalize_crypto_pay_api_base(
            crypto_pay_api_base_url
        )
        self.crypto_pay_webhook_secret = crypto_pay_webhook_secret.strip()
        self.crypto_pay_accepted_assets = self._normalize_crypto_pay_assets(
            crypto_pay_accepted_assets
        )
        self.crypto_pay_request_timeout_seconds = crypto_pay_request_timeout_seconds
        self.telegram_stars_amount = max(0, int(telegram_stars_amount))
        self.plans = PlanRepository()
        self.payments = PaymentRepository()
        self.subscriptions = SubscriptionRepository()
        self.webhooks = WebhookLogRepository()
        self.coins = CoinService()
        self.referrals = referrals or ReferralService(db)

    def create_payment(
        self, *, user_id: int, plan_code: str, payment_method: str = ""
    ) -> Dict[str, Any]:
        normalized_method = (payment_method or "").strip().lower()
        if normalized_method in TELEGRAM_STARS_METHODS:
            return self.create_telegram_stars_payment(
                user_id=user_id,
                plan_code=plan_code,
                payment_method=normalized_method,
            )
        if normalized_method in CRYPTO_PAY_METHODS:
            return self.create_crypto_pay_payment(
                user_id=user_id,
                plan_code=plan_code,
                payment_method=normalized_method,
            )
        if normalized_method in YOOKASSA_METHODS:
            return self.create_yookassa_payment(user_id=user_id, plan_code=plan_code)
        if self.payment_provider == CRYPTO_PAY_PROVIDER:
            return self.create_crypto_pay_payment(
                user_id=user_id,
                plan_code=plan_code,
                payment_method=normalized_method or CRYPTO_PAY_PROVIDER,
            )
        if self.payment_provider == TELEGRAM_STARS_PROVIDER:
            return self.create_telegram_stars_payment(
                user_id=user_id,
                plan_code=plan_code,
                payment_method=normalized_method or TELEGRAM_STARS_PROVIDER,
            )
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

    def create_telegram_stars_payment(
        self, *, user_id: int, plan_code: str, payment_method: str = "telegram_stars"
    ) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            plan = self.plans.get_by_code(conn, plan_code)
            if plan is None or not plan["is_active"]:
                raise NotFoundError("Тариф не найден")
            external_id = f"stars_{uuid.uuid4().hex}"
            stars_amount = self._telegram_stars_amount(plan)
            return self.payments.create_pending(
                conn,
                user_id=user_id,
                plan_id=plan["id"],
                amount_rub=plan["price_rub"],
                external_id=external_id,
                payment_url=f"telegram-stars://{external_id}",
                provider=TELEGRAM_STARS_PROVIDER,
                meta={
                    "kind": "telegram_stars_invoice",
                    "payment_method": payment_method or TELEGRAM_STARS_PROVIDER,
                    "plan_code": plan["code"],
                    "plan_name": plan["name"],
                    "coins_amount": plan["coins_amount"],
                    "duration_days": plan["duration_days"],
                    "price_rub": plan["price_rub"],
                    "stars_amount": stars_amount,
                    "stars_fixed_amount": self.telegram_stars_amount,
                },
            )

    def create_crypto_pay_payment(
        self, *, user_id: int, plan_code: str, payment_method: str = "crypto_pay"
    ) -> Dict[str, Any]:
        if not self.crypto_pay_token:
            raise BusinessRuleError("Crypto Pay не настроен: нужен CRYPTO_PAY_TOKEN.")

        with self.db.transaction() as conn:
            plan = self.plans.get_by_code(conn, plan_code)
            if plan is None or not plan["is_active"]:
                raise NotFoundError("Тариф не найден")

        amount_rub = int(plan["price_rub"])
        idempotence_key = f"ceaai-crypto-{uuid.uuid4().hex}"
        payload = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(amount_rub),
            "accepted_assets": self.crypto_pay_accepted_assets,
            "description": self._payment_description(plan),
            "payload": idempotence_key,
            "paid_btn_name": "openBot",
            "paid_btn_url": "https://t.me/aiceabot",
        }
        response = self._crypto_pay_request("createInvoice", payload=payload)
        invoice = response.get("result")
        if not isinstance(invoice, dict):
            raise BusinessRuleError("Crypto Pay вернул некорректный invoice.")
        external_id = str(invoice.get("invoice_id") or "")
        payment_url = str(
            invoice.get("pay_url")
            or invoice.get("bot_invoice_url")
            or invoice.get("mini_app_invoice_url")
            or invoice.get("web_app_invoice_url")
            or ""
        )
        if not external_id or not payment_url:
            raise BusinessRuleError("Crypto Pay не вернул ссылку на оплату.")

        with self.db.transaction() as conn:
            return self.payments.create_pending(
                conn,
                user_id=user_id,
                plan_id=plan["id"],
                amount_rub=amount_rub,
                external_id=external_id,
                payment_url=payment_url,
                provider=CRYPTO_PAY_PROVIDER,
                meta={
                    "kind": "crypto_pay_invoice",
                    "payment_method": payment_method or CRYPTO_PAY_PROVIDER,
                    "idempotence_key": idempotence_key,
                    "accepted_assets": self.crypto_pay_accepted_assets,
                    "response": invoice,
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
            "save_payment_method": True,
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

    def process_due_auto_renewals(
        self, *, limit: int = AUTO_RENEWAL_BATCH_SIZE
    ) -> list[PaymentWebhookResult]:
        now = utcnow()
        due_at = (now + timedelta(seconds=AUTO_RENEWAL_LOOKAHEAD_SECONDS)).isoformat()
        retry_before = (
            now - timedelta(seconds=AUTO_RENEWAL_RETRY_INTERVAL_SECONDS)
        ).isoformat()
        with self.db.transaction() as conn:
            due_subscriptions = self.subscriptions.list_due_auto_renewals(
                conn,
                due_at=due_at,
                retry_before=retry_before,
                limit=limit,
            )

        results: list[PaymentWebhookResult] = []
        for subscription in due_subscriptions:
            try:
                result = self.create_yookassa_auto_renewal_payment(
                    subscription=subscription,
                )
            except Exception as exc:
                logging.warning(
                    "YooKassa auto renewal failed for subscription %s: %s",
                    subscription.get("id"),
                    exc,
                )
                with self.db.transaction() as conn:
                    self.subscriptions.mark_auto_renew_attempt(
                        conn,
                        subscription_id=int(subscription["id"]),
                        error_message=str(exc)[:500],
                    )
                continue
            results.append(result)
        return results

    def create_yookassa_auto_renewal_payment(
        self, *, subscription: Dict[str, Any]
    ) -> PaymentWebhookResult:
        payment_method_id = str(
            subscription.get("yookassa_payment_method_id") or ""
        ).strip()
        if not payment_method_id:
            raise BusinessRuleError("У подписки нет сохранённого способа оплаты.")

        plan = {
            "id": int(subscription["plan_id"]),
            "name": str(subscription.get("plan_name") or "тариф"),
            "code": str(subscription.get("plan_code") or ""),
            "price_rub": int(subscription.get("plan_price_rub") or 0),
            "duration_days": int(subscription.get("plan_duration_days") or 30),
            "coins_amount": int(subscription.get("plan_coins_amount") or 0),
        }
        amount_rub = int(plan["price_rub"])
        idempotence_key = f"ceaai-auto-renew-{subscription['id']}-{uuid.uuid4().hex}"
        payload = {
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB",
            },
            "capture": True,
            "payment_method_id": payment_method_id,
            "description": self._payment_description(plan),
            "metadata": {
                "kind": "subscription_auto_renewal",
                "user_id": str(subscription["user_id"]),
                "plan_id": str(plan["id"]),
                "plan_code": str(plan["code"]),
                "subscription_id": str(subscription["id"]),
                "idempotence_key": idempotence_key,
            },
        }

        with self.db.transaction() as conn:
            self.subscriptions.mark_auto_renew_attempt(
                conn,
                subscription_id=int(subscription["id"]),
                error_message=None,
            )

        response = self._yookassa_request(
            "POST",
            "/payments",
            payload=payload,
            idempotence_key=idempotence_key,
        )
        external_id = str(response.get("id") or "")
        if not external_id:
            raise BusinessRuleError("ЮKassa не вернула id автопродления.")

        with self.db.transaction() as conn:
            self.payments.create_pending(
                conn,
                user_id=int(subscription["user_id"]),
                plan_id=plan["id"],
                amount_rub=amount_rub,
                external_id=external_id,
                payment_url=f"yookassa://auto-renewal/{external_id}",
                provider=YOOKASSA_PROVIDER,
                meta={
                    "kind": "yookassa_auto_renewal",
                    "subscription_id": subscription["id"],
                    "yookassa_payment_method_id": payment_method_id,
                    "idempotence_key": idempotence_key,
                    "request": payload,
                    "response": response,
                },
            )

        if not self._is_yookassa_payment_succeeded(response):
            return PaymentWebhookResult(
                processed=False,
                duplicate=False,
                payment=None,
                subscription=None,
                credited_coins=0,
                message="Auto renewal payment created and waits for webhook",
            )

        webhook, should_process, duplicate = self._reserve_webhook(
            provider=YOOKASSA_PROVIDER,
            external_id=external_id,
            event_type="payment.succeeded",
            payload={
                "event": "payment.succeeded",
                "object": {"id": external_id},
                "auto_renewal": response,
            },
        )
        if not should_process:
            return PaymentWebhookResult(
                processed=False,
                duplicate=duplicate,
                payment=None,
                subscription=None,
                credited_coins=0,
                message="Auto renewal already processed or received",
            )
        return self._process_successful_payment(
            provider=YOOKASSA_PROVIDER,
            external_id=external_id,
            webhook_id=int(webhook["id"]),
            payload={
                "webhook": {
                    "event": "payment.succeeded",
                    "object": {"id": external_id},
                },
                "verified_payment": response,
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

    def process_crypto_pay_webhook(
        self,
        *,
        payload: Dict[str, Any],
        raw_body: bytes,
        signature: str,
    ) -> PaymentWebhookResult:
        self._verify_crypto_pay_signature(raw_body=raw_body, signature=signature)
        event_type = str(payload.get("update_type") or "")
        invoice = payload.get("payload")
        if not isinstance(invoice, dict):
            raise BusinessRuleError("Некорректный Crypto Pay webhook: нет invoice.")
        external_id = str(invoice.get("invoice_id") or "")
        if not external_id:
            raise BusinessRuleError(
                "Некорректный Crypto Pay webhook: нет invoice_id."
            )

        webhook, should_process, duplicate = self._reserve_webhook(
            provider=CRYPTO_PAY_PROVIDER,
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

        status = str(invoice.get("status") or "")
        if event_type != "invoice_paid" or status != "paid":
            self._mark_webhook(
                webhook_id=int(webhook["id"]),
                status="ignored",
                error_message=f"Unsupported Crypto Pay event {event_type}/{status}",
            )
            return PaymentWebhookResult(
                processed=False,
                duplicate=False,
                payment=None,
                subscription=None,
                credited_coins=0,
                message=f"Crypto Pay webhook ignored: {event_type}/{status}",
            )

        return self._process_successful_payment(
            provider=CRYPTO_PAY_PROVIDER,
            external_id=external_id,
            webhook_id=int(webhook["id"]),
            payload={
                "webhook": payload,
                "invoice": invoice,
            },
        )

    def validate_telegram_stars_pre_checkout(
        self, *, invoice_payload: str, currency: str, total_amount: int
    ) -> Dict[str, Any]:
        payment = self._load_telegram_stars_payment(invoice_payload=invoice_payload)
        if payment["status"] != "pending":
            raise BusinessRuleError("Этот счёт уже обработан или недоступен.")
        self._validate_telegram_stars_amount(
            payment=payment, currency=currency, total_amount=total_amount
        )
        return payment

    def process_telegram_stars_successful_payment(
        self,
        *,
        invoice_payload: str,
        currency: str,
        total_amount: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str = "",
    ) -> PaymentWebhookResult:
        payment = self._load_telegram_stars_payment(invoice_payload=invoice_payload)
        if payment["status"] not in {"pending", "paid"}:
            raise BusinessRuleError(f"Статус счёта: {payment['status']}.")
        self._validate_telegram_stars_amount(
            payment=payment, currency=currency, total_amount=total_amount
        )
        payload = {
            "provider": TELEGRAM_STARS_PROVIDER,
            "invoice_payload": invoice_payload,
            "currency": currency,
            "total_amount": total_amount,
            "telegram_payment_charge_id": telegram_payment_charge_id,
            "provider_payment_charge_id": provider_payment_charge_id,
        }
        webhook, should_process, duplicate = self._reserve_webhook(
            provider=TELEGRAM_STARS_PROVIDER,
            external_id=invoice_payload,
            event_type="successful_payment",
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
            provider=TELEGRAM_STARS_PROVIDER,
            external_id=invoice_payload,
            webhook_id=int(webhook["id"]),
            payload=payload,
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
            payment_meta = loads_dict(payment.get("meta"))
            paid_meta = {
                **payment_meta,
                **payload,
            }
            payment = self.payments.mark_paid(
                conn,
                payment_id=payment["id"],
                subscription_id=subscription["id"],
                meta=paid_meta,
            )
            yookassa_payment_method_id = self._yookassa_payment_method_id(
                payload=payload,
                payment_meta=payment_meta,
            )
            if provider == YOOKASSA_PROVIDER and yookassa_payment_method_id:
                subscription = self.subscriptions.configure_auto_renew(
                    conn,
                    subscription_id=subscription["id"],
                    payment_method_id=yookassa_payment_method_id,
                    is_active=True,
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
        response_body = self._open_yookassa_request(request)

        data = json.loads(response_body) if response_body else {}
        if not isinstance(data, dict):
            raise BusinessRuleError("ЮKassa вернула некорректный ответ.")
        return data

    def _open_yookassa_request(self, request: urllib.request.Request) -> str:
        last_error: BaseException | None = None
        for attempt in range(1, YOOKASSA_NETWORK_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.yookassa_request_timeout_seconds,
                ) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                logging.warning("YooKassa API error %s: %s", exc.code, error_body)
                raise BusinessRuleError(self._yookassa_http_error_message(exc)) from exc
            except YOOKASSA_NETWORK_ERRORS as exc:
                last_error = exc
                if attempt < YOOKASSA_NETWORK_ATTEMPTS:
                    time.sleep(0.5 * attempt)
                    continue
                logging.warning(
                    "YooKassa API unavailable after %s attempts: %r",
                    YOOKASSA_NETWORK_ATTEMPTS,
                    exc,
                )
        raise BusinessRuleError(
            "ЮKassa сейчас не отвечает. Попробуйте оплатить ещё раз через минуту."
        ) from last_error

    @staticmethod
    def _yookassa_http_error_message(exc: urllib.error.HTTPError) -> str:
        if exc.code in {401, 403}:
            return (
                "ЮKassa отклонила запрос. Проверьте Shop ID и секретный ключ "
                "в настройках хостинга."
            )
        if exc.code == 400:
            return (
                "ЮKassa отклонила платёж. Проверьте настройки магазина и "
                "доступные способы оплаты."
            )
        return "ЮKassa вернула ошибку. Попробуйте оплатить ещё раз позже."

    def _yookassa_auth_header(self) -> str:
        raw = f"{self.yookassa_shop_id}:{self.yookassa_secret_key}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _yookassa_payment_method_id(
        *, payload: Dict[str, Any], payment_meta: Dict[str, Any]
    ) -> str:
        verified_payment = payload.get("verified_payment")
        if isinstance(verified_payment, dict):
            payment_method = verified_payment.get("payment_method")
            if isinstance(payment_method, dict):
                method_id = str(payment_method.get("id") or "").strip()
                if method_id and payment_method.get("saved") is not False:
                    return method_id
        return str(payment_meta.get("yookassa_payment_method_id") or "").strip()

    def _crypto_pay_request(
        self, method_name: str, *, payload: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self.crypto_pay_token:
            raise BusinessRuleError("Crypto Pay не настроен.")

        url = f"{self.crypto_pay_api_base_url}/{method_name.lstrip('/')}"
        body = None
        headers = {
            "Accept": "application/json",
            "Crypto-Pay-API-Token": self.crypto_pay_token,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=self.crypto_pay_request_timeout_seconds
            ) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise BusinessRuleError(
                f"Crypto Pay API error {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BusinessRuleError(f"Crypto Pay API недоступна: {exc.reason}") from exc

        data = json.loads(response_body) if response_body else {}
        if not isinstance(data, dict):
            raise BusinessRuleError("Crypto Pay вернул некорректный ответ.")
        if not data.get("ok"):
            raise BusinessRuleError(f"Crypto Pay API error: {data}")
        return data

    def _verify_crypto_pay_signature(self, *, raw_body: bytes, signature: str) -> None:
        if not self.crypto_pay_token:
            raise BusinessRuleError("Crypto Pay не настроен.")
        received = (signature or "").strip().lower()
        if not received:
            raise BusinessRuleError("Crypto Pay webhook без подписи.")
        secret = hashlib.sha256(self.crypto_pay_token.encode("utf-8")).digest()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise BusinessRuleError("Некорректная подпись Crypto Pay webhook.")

    def _load_telegram_stars_payment(self, *, invoice_payload: str) -> Dict[str, Any]:
        if not invoice_payload:
            raise BusinessRuleError("Некорректный Telegram Stars payload.")
        with self.db.transaction() as conn:
            payment = self.payments.get_by_external_id(
                conn, TELEGRAM_STARS_PROVIDER, invoice_payload
            )
        if payment is None:
            raise NotFoundError("Счёт Telegram Stars не найден.")
        return payment

    def _validate_telegram_stars_amount(
        self, *, payment: Dict[str, Any], currency: str, total_amount: int
    ) -> None:
        if currency != "XTR":
            raise BusinessRuleError("Некорректная валюта Telegram Stars.")
        expected_amount = self._payment_telegram_stars_amount(payment)
        if int(total_amount) != expected_amount:
            raise BusinessRuleError("Некорректная сумма Telegram Stars.")

    def _public_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.app_base_url}{normalized_path}"

    @staticmethod
    def _normalize_crypto_pay_api_base(value: str) -> str:
        cleaned = (value or "https://testnet-pay.crypt.bot/api").strip().rstrip("/")
        if not cleaned.endswith("/api"):
            cleaned += "/api"
        return cleaned

    @staticmethod
    def _normalize_crypto_pay_assets(value: str) -> str:
        assets = [asset.strip().upper() for asset in (value or "").split(",")]
        cleaned = [asset for asset in assets if asset]
        return ",".join(cleaned) or "USDT"

    def _telegram_stars_amount(self, plan: Dict[str, Any]) -> int:
        if self.telegram_stars_amount > 1:
            return self.telegram_stars_amount
        return telegram_stars_amount_for_rub(int(plan["price_rub"]))

    @staticmethod
    def _payment_telegram_stars_amount(payment: Dict[str, Any]) -> int:
        meta = loads_dict(payment.get("meta"))
        return max(1, int(meta.get("stars_amount") or payment["amount_rub"]))

    @staticmethod
    def _payment_description(plan: Dict[str, Any]) -> str:
        description = f"CeaAI: тариф {plan['name']} на {plan['duration_days']} дней"
        return description[:128]

    @staticmethod
    def _is_yookassa_payment_succeeded(payment: Dict[str, Any]) -> bool:
        return payment.get("status") == "succeeded" and bool(payment.get("paid"))
