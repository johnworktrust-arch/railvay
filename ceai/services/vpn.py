from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from ceai.database import Database
from ceai.repositories.vpn_payments import VpnPaymentRepository
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceai.repositories.vpn_trial_claims import VpnTrialClaimRepository
from ceai.services.exceptions import BusinessRuleError
from ceai.services.platega import (
    PLATEGA_CANCELED,
    PLATEGA_CHARGEBACKED,
    PLATEGA_CONFIRMED,
    PLATEGA_PENDING,
    PlategaClient,
    PlategaError,
    PlategaTransaction,
)
from ceai.services.referrals import ReferralService
from ceai.time_utils import parse_iso, utcnow


PLATEGA_PROVIDER = "platega"
PLATEGA_PAYMENT_METHOD = "platega"
_PLATEGA_FALLBACK_LINK_LIFETIME = timedelta(minutes=15)
_PLATEGA_EXPIRES_IN_PATTERN = re.compile(
    r"^(?P<hours>\d{1,3}):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)$"
)
_PLATEGA_RECONCILIATION_BATCH_SIZE = 20
_PLATEGA_FAILED_RECHECK_WINDOW = timedelta(minutes=10)
_PLATEGA_PAID_DISPUTE_HORIZON = timedelta(days=400)
_PLATEGA_PAID_RECONCILIATION_LIMIT = 2
_MARZBAN_VLESS_INBOUND_TAGS = (
    "VLESS TCP REALITY",
    "VLESS WS TLS FALLBACK",
)
_MARZBAN_PROFILE_VERSION = "v2"


class VpnPaymentVerificationError(BusinessRuleError):
    """A permanent provider/order mismatch that callback retries cannot fix."""


@dataclass(frozen=True)
class VpnTrialOutcome:
    subscription: Dict[str, Any]
    created: bool
    trial_already_used: bool = False


@dataclass(frozen=True)
class VpnPaymentOutcome:
    payment: Dict[str, Any]
    subscription: Dict[str, Any]
    processed: bool


@dataclass(frozen=True)
class VpnPaymentStatusOutcome:
    payment: Dict[str, Any] | None
    subscription: Dict[str, Any] | None
    processed: bool
    status: str

    @property
    def confirmed(self) -> bool:
        return self.status == "paid"


@dataclass(frozen=True)
class VpnJobCompletion:
    subscription: Dict[str, Any]
    telegram_id: int
    operation: str


class VpnService:
    def __init__(
        self,
        db: Database,
        *,
        server_code: str,
        trial_days: int = 3,
        allow_admin_demo_payment: bool = False,
        payment_provider: str = "disabled",
        app_base_url: str = "",
        platega_merchant_id: str = "",
        platega_secret: str = "",
        platega_api_base_url: str = "https://app.platega.io",
        platega_return_path: str = "/payments/vpn/platega/return",
        platega_failed_path: str = "/payments/vpn/platega/failed",
        platega_request_timeout_seconds: int = 30,
        platega_client: PlategaClient | None = None,
        worker_health_max_age_seconds: int = 120,
        referrals: ReferralService | None = None,
    ) -> None:
        if (
            isinstance(worker_health_max_age_seconds, bool)
            or not isinstance(worker_health_max_age_seconds, int)
            or worker_health_max_age_seconds <= 0
        ):
            raise ValueError("worker_health_max_age_seconds must be positive")
        self.db = db
        self.server_code = server_code
        self.trial_days = trial_days
        self.allow_admin_demo_payment = allow_admin_demo_payment
        self.payment_provider = payment_provider.strip().lower() or "disabled"
        self.app_base_url = app_base_url.strip().rstrip("/")
        self.platega_return_path = self._normalize_path(platega_return_path)
        self.platega_failed_path = self._normalize_path(platega_failed_path)
        self.platega_client = platega_client
        self.worker_health_max_age_seconds = worker_health_max_age_seconds
        self.referrals = referrals or ReferralService(db)
        if (
            self.platega_client is None
            and self.payment_provider == PLATEGA_PROVIDER
            and platega_merchant_id
            and platega_secret
        ):
            self.platega_client = PlategaClient(
                platega_merchant_id,
                platega_secret,
                api_base_url=platega_api_base_url,
                timeout_seconds=platega_request_timeout_seconds,
            )
        self.servers = VpnServerRepository()
        self.plans = VpnPlanRepository()
        self.payments = VpnPaymentRepository()
        self.subscriptions = VpnSubscriptionRepository()
        self.trials = VpnTrialClaimRepository()
        self.jobs = VpnProvisioningJobRepository()

    def claim_trial(
        self,
        *,
        user_id: int,
        channel: str,
    ) -> VpnTrialOutcome:
        if self.trial_days <= 0:
            raise BusinessRuleError("Бесплатный период временно недоступен.")

        with self.db.transaction() as conn:
            existing_claim = self.trials.get_by_user_id(conn, user_id)
            if existing_claim is not None:
                subscription = self.subscriptions.get_by_id(
                    conn, int(existing_claim["subscription_id"])
                )
                if subscription is None:
                    raise RuntimeError("VPN trial points to a missing subscription")
                return VpnTrialOutcome(
                    subscription=subscription,
                    created=False,
                    trial_already_used=True,
                )

            live = self.subscriptions.get_live_for_user(conn, user_id)
            if live is not None:
                subscription = self.subscriptions.get_by_id(conn, int(live["id"]))
                if subscription is None:
                    raise RuntimeError("VPN subscription disappeared")
                return VpnTrialOutcome(subscription=subscription, created=False)

            server = self._require_checkout_ready_server(conn)

            starts_at = utcnow()
            subscription = self.subscriptions.create_provisioning(
                conn,
                user_id=user_id,
                server_id=int(server["id"]),
                plan_id=None,
                kind="trial",
                provider_username=f"u_{secrets.token_hex(12)}",
                starts_at=starts_at.isoformat(),
                ends_at=(starts_at + timedelta(days=self.trial_days)).isoformat(),
            )
            self.trials.create(
                conn,
                user_id=user_id,
                subscription_id=int(subscription["id"]),
                channel=channel,
            )
            self.jobs.enqueue(
                conn,
                subscription_id=int(subscription["id"]),
                operation="create",
                idempotency_key=f"vpn:create:{subscription['id']}",
            )
            return VpnTrialOutcome(subscription=subscription, created=True)

    def create_admin_demo_payment(
        self,
        *,
        user_id: int,
        plan_code: str,
        payment_method: str,
        admin_authorized: bool,
    ) -> tuple[Dict[str, Any], bool]:
        self._require_admin_demo_access(admin_authorized)
        method = payment_method.strip().lower()
        if method not in {"sbp", "card", "crypto", "stars", "other"}:
            raise BusinessRuleError("Неизвестный способ оплаты.")

        with self.db.transaction() as conn:
            plan = self.plans.get_by_code(conn, plan_code)
            if plan is None or not bool(plan["is_active"]):
                raise BusinessRuleError("Тариф не найден.")
            return self.payments.create_or_get_pending_admin_demo(
                conn,
                user_id=user_id,
                plan_id=int(plan["id"]),
                amount_rub=int(plan["price_rub"]),
                duration_days=int(plan["duration_days"]),
                payment_method=method,
            )

    @property
    def uses_platega(self) -> bool:
        return self.payment_provider == PLATEGA_PROVIDER

    def create_platega_payment(
        self,
        *,
        user_id: int,
        plan_code: str,
        user_name: str = "",
    ) -> tuple[Dict[str, Any], bool]:
        client = self._require_platega()
        if not self.app_base_url:
            raise BusinessRuleError(
                "Оплата временно недоступна: не настроен адрес приложения."
            )

        # The second pass is used only after an expired provider transaction is
        # verified and closed. It prevents an old URL from being reused forever
        # while keeping all provider calls outside database transactions.
        for _attempt in range(2):
            request_external_id = (
                f"platega_request_{secrets.token_urlsafe(18)}"
            )
            with self.db.transaction() as conn:
                plan = self.plans.get_by_code(conn, plan_code)
                if plan is None or not bool(plan["is_active"]):
                    raise BusinessRuleError("Тариф не найден.")
                payment, created = self.payments.create_or_get_pending_platega(
                    conn,
                    user_id=user_id,
                    plan_id=int(plan["id"]),
                    amount_rub=int(plan["price_rub"]),
                    duration_days=int(plan["duration_days"]),
                    payment_method=PLATEGA_PAYMENT_METHOD,
                    request_external_id=request_external_id,
                )
                if created or not payment.get("payment_url"):
                    # A new checkout must not accept money while the outbound
                    # provisioning worker is stale or administratively off.
                    # An already-issued valid provider URL remains reusable.
                    self._require_checkout_ready_server(conn)
                plan_name = str(plan["name"])

            if payment.get("payment_url"):
                if not self._platega_payment_link_expired(payment):
                    return payment, created
                outcome = self._refresh_expired_platega_payment(
                    client=client,
                    payment=payment,
                    user_id=user_id,
                )
                if outcome.status == "paid":
                    # Do not open another order after a late confirmation: that
                    # could charge the customer twice.
                    assert outcome.payment is not None
                    return outcome.payment, False
                continue

            try:
                remote = client.create_payment(
                    amount_rub=int(payment["amount_rub"]),
                    description=f"CEA VPN — {plan_name}, до 3 устройств",
                    return_url=self._public_url(self.platega_return_path),
                    failed_url=self._public_url(self.platega_failed_path),
                    payload=f"vpn_payment:{int(payment['id'])}",
                    user_id=user_id,
                    user_name=user_name,
                )
            except PlategaError as exc:
                raise BusinessRuleError(
                    "Платёжный сервис сейчас недоступен. Попробуйте ещё раз через минуту."
                ) from exc

            expires_at = self._platega_expires_at(remote.expires_in)
            try:
                with self.db.transaction() as conn:
                    attached = self.payments.attach_platega_transaction(
                        conn,
                        payment_id=int(payment["id"]),
                        user_id=user_id,
                        expected_external_id=str(payment["external_id"]),
                        external_id=remote.transaction_id,
                        payment_url=remote.payment_url,
                        expires_at=expires_at,
                    )
                return attached, created
            except (RuntimeError, ValueError):
                # A concurrent click may have attached the same local order
                # first. Its URL is authoritative; the losing remote
                # transaction remains unlinked and can never provision a key.
                with self.db.transaction() as conn:
                    current = self.payments.get_for_user(
                        conn, int(payment["id"]), user_id
                    )
                if current is not None and current.get("payment_url"):
                    return current, False
                raise

        raise BusinessRuleError(
            "Не удалось обновить ссылку на оплату. Попробуйте ещё раз."
        )

    def check_platega_payment(
        self,
        *,
        user_id: int,
        payment_id: int,
    ) -> VpnPaymentStatusOutcome:
        client = self._require_platega()
        with self.db.transaction() as conn:
            payment = self.payments.get_for_user(conn, payment_id, user_id)
            if payment is None or payment.get("provider") != PLATEGA_PROVIDER:
                raise BusinessRuleError("Заказ не найден.")
            local_outcome = self._existing_payment_outcome(conn, payment)
            # A locally paid order is still re-fetched: otherwise a missed
            # CHARGEBACKED callback would leave access running indefinitely.
            if local_outcome is not None and payment.get("status") not in {
                "failed",
                "paid",
            }:
                return local_outcome
            external_id = str(payment.get("external_id") or "")
            if external_id.startswith("platega_request_"):
                raise BusinessRuleError(
                    "Ссылка на оплату ещё создаётся. Попробуйте ещё раз."
                )

        try:
            remote = client.get_transaction(external_id)
        except PlategaError as exc:
            raise BusinessRuleError(
                "Не удалось проверить оплату. Попробуйте ещё раз через минуту."
            ) from exc
        return self._apply_verified_platega_transaction(
            payment_id=payment_id,
            remote=remote,
            expected_user_id=user_id,
        )

    def reconcile_platega_payments(
        self,
        *,
        batch_size: int = _PLATEGA_RECONCILIATION_BATCH_SIZE,
        failed_recheck_window: timedelta = _PLATEGA_FAILED_RECHECK_WINDOW,
        paid_dispute_horizon: timedelta = _PLATEGA_PAID_DISPUTE_HORIZON,
    ) -> int:
        """Reconcile a bounded, rotating slice of attached Platega orders.

        Provider calls happen after the candidate-selection transaction has
        closed. Pending rows are touched after each attempt so an unavailable
        transaction cannot starve the queue. Recent synthetic ``failed`` rows
        rotate by ID instead: their ``updated_at`` remains the original close
        time and therefore cannot keep extending the eventual-confirm window.
        A small circular slice of paid orders inside the finite dispute horizon
        catches chargebacks whose callback was missed without unbounded polling.

        Returns the number of local payment/subscription state transitions.
        """

        if self.payment_provider != PLATEGA_PROVIDER:
            return 0
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError("batch_size must be an integer")
        if batch_size < 2 or batch_size > 100:
            raise ValueError("batch_size must be between 2 and 100")
        if (
            not isinstance(failed_recheck_window, timedelta)
            or failed_recheck_window <= timedelta(0)
            or failed_recheck_window > timedelta(hours=1)
        ):
            raise ValueError("failed_recheck_window must be between 0 and 1 hour")
        if (
            not isinstance(paid_dispute_horizon, timedelta)
            or paid_dispute_horizon <= timedelta(0)
            or paid_dispute_horizon > timedelta(days=730)
        ):
            raise ValueError("paid_dispute_horizon must be between 0 and 730 days")

        client = self._require_platega()
        failed_limit = max(1, batch_size // 4)
        paid_limit = min(
            _PLATEGA_PAID_RECONCILIATION_LIMIT,
            max(1, batch_size // 10),
        )
        failed_since = (utcnow() - failed_recheck_window).isoformat()
        paid_since = (utcnow() - paid_dispute_horizon).isoformat()
        failed_after_id = int(
            getattr(self, "_platega_failed_reconciliation_cursor", 0)
        )
        paid_after_id = int(
            getattr(self, "_platega_paid_reconciliation_cursor", 0)
        )
        with self.db.transaction() as conn:
            paid_candidates = (
                self.payments.list_recent_paid_platega_for_reconciliation(
                    conn,
                    paid_since=paid_since,
                    after_id=paid_after_id,
                    limit=paid_limit,
                )
            )
            failed_candidates = (
                self.payments.list_recent_failed_platega_for_reconciliation(
                    conn,
                    failed_since=failed_since,
                    after_id=failed_after_id,
                    limit=failed_limit,
                )
            )
            pending_candidates = (
                self.payments.list_attached_pending_platega_for_reconciliation(
                    conn,
                    limit=(
                        batch_size
                        - len(failed_candidates)
                        - len(paid_candidates)
                    ),
                )
            )

        transitioned = 0
        for payment in [
            *failed_candidates,
            *pending_candidates,
            *paid_candidates,
        ]:
            payment_id = int(payment["id"])
            external_id = str(payment.get("external_id") or "")
            original_status = str(payment.get("status") or "")
            try:
                remote = client.get_transaction(external_id)
                outcome = self._apply_verified_platega_transaction(
                    payment_id=payment_id,
                    remote=remote,
                    expected_user_id=int(payment["user_id"]),
                )
                if outcome.processed:
                    transitioned += 1
                if (
                    original_status == "pending"
                    and outcome.status == "pending"
                    and outcome.payment is not None
                    and self._platega_payment_link_expired(outcome.payment)
                ):
                    closed = self._close_expired_platega_payment(
                        payment_id=payment_id,
                        external_id=external_id,
                        user_id=int(payment["user_id"]),
                    )
                    if closed.processed:
                        transitioned += 1
            except Exception as exc:
                # Provider and verification errors are isolated to this order.
                # Log only the local ID and exception class; provider response
                # bodies and credentials must never reach maintenance logs.
                logging.warning(
                    "Platega VPN reconciliation failed for payment_id=%s (%s)",
                    payment_id,
                    type(exc).__name__,
                )
            finally:
                if original_status == "failed":
                    self._platega_failed_reconciliation_cursor = payment_id
                elif original_status == "paid":
                    self._platega_paid_reconciliation_cursor = payment_id
                elif original_status == "pending":
                    try:
                        with self.db.transaction() as conn:
                            self.payments.touch_pending_platega_reconciliation(
                                conn,
                                payment_id=payment_id,
                                external_id=external_id,
                            )
                    except Exception as exc:
                        logging.warning(
                            "Could not rotate Platega VPN payment_id=%s (%s)",
                            payment_id,
                            type(exc).__name__,
                        )
        return transitioned

    def handle_platega_callback(
        self,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> VpnPaymentStatusOutcome:
        client = self._require_platega()
        client.authenticate_callback(headers)
        raw_external_id = payload.get("id") or payload.get("transactionId")
        if not isinstance(raw_external_id, str) or not raw_external_id.strip():
            raise VpnPaymentVerificationError(
                "Некорректный Platega callback."
            )
        external_id = raw_external_id.strip()
        try:
            if str(uuid.UUID(external_id)) != external_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise VpnPaymentVerificationError(
                "Некорректный Platega callback."
            ) from None

        with self.db.transaction() as conn:
            payment = self.payments.get_by_provider_external_id(
                conn,
                provider=PLATEGA_PROVIDER,
                external_id=external_id,
            )
            if payment is None:
                # The merchant may have other products in the same account.
                # Unknown transactions must never create a VPN order.
                return VpnPaymentStatusOutcome(
                    payment=None,
                    subscription=None,
                    processed=False,
                    status="ignored",
                )
            payment_id = int(payment["id"])

        # Callback fields are only a notification. The provider API is the
        # authoritative source for status, amount and currency.
        remote = client.get_transaction(external_id)
        return self._apply_verified_platega_transaction(
            payment_id=payment_id,
            remote=remote,
            expected_user_id=None,
        )

    def get_payment_for_user(
        self, *, user_id: int, payment_id: int
    ) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.payments.get_for_user(conn, payment_id, user_id)

    def get_payment_subscription_for_user(
        self, *, user_id: int, payment_id: int
    ) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            payment = self.payments.get_for_user(conn, payment_id, user_id)
            if payment is None or payment.get("status") != "paid":
                return None
            subscription_id = payment.get("vpn_subscription_id")
            if subscription_id is None:
                return None
            subscription = self.subscriptions.get_by_id(
                conn, int(subscription_id)
            )
            if (
                subscription is None
                or int(subscription["user_id"]) != int(user_id)
            ):
                return None
            return subscription

    def confirm_admin_demo_payment(
        self,
        *,
        user_id: int,
        payment_id: int,
        admin_authorized: bool,
    ) -> VpnPaymentOutcome:
        self._require_admin_demo_access(admin_authorized)
        with self.db.transaction() as conn:
            payment = self.payments.get_for_user(conn, payment_id, user_id)
            if payment is None or payment.get("provider") != "admin_demo":
                raise BusinessRuleError("Заказ не найден.")
            if payment.get("status") not in {"pending", "paid"}:
                raise BusinessRuleError("Этот заказ уже закрыт.")

            plan = self.plans.get_by_id(conn, int(payment["vpn_plan_id"]))
            if (
                plan is None
                or payment.get("currency") != "RUB"
                or int(payment["duration_days"]) <= 0
            ):
                raise BusinessRuleError("Параметры заказа повреждены.")

            payment, marked_paid = self.payments.mark_admin_demo_paid(
                conn,
                payment_id=payment_id,
                user_id=user_id,
            )
            subscription_id = payment.get("vpn_subscription_id")
            if subscription_id is not None:
                subscription = self.subscriptions.get_by_id(
                    conn, int(subscription_id)
                )
                if (
                    subscription is None
                    or int(subscription["user_id"]) != int(user_id)
                ):
                    raise RuntimeError("VPN payment points to an invalid subscription")
                return VpnPaymentOutcome(
                    payment=payment,
                    subscription=subscription,
                    processed=False,
                )

            subscription = self._fulfill_paid_payment(
                conn,
                payment=payment,
                plan=plan,
            )
            payment = self.payments.link_subscription(
                conn,
                payment_id=payment_id,
                user_id=user_id,
                subscription_id=int(subscription["id"]),
            )
            return VpnPaymentOutcome(
                payment=payment,
                subscription=subscription,
                processed=bool(marked_paid),
            )

    def _apply_verified_platega_transaction(
        self,
        *,
        payment_id: int,
        remote: PlategaTransaction,
        expected_user_id: int | None,
    ) -> VpnPaymentStatusOutcome:
        with self.db.transaction() as conn:
            payment = self.payments.get_by_id(conn, payment_id)
            if (
                payment is None
                or payment.get("provider") != PLATEGA_PROVIDER
                or (
                    expected_user_id is not None
                    and int(payment["user_id"]) != int(expected_user_id)
                )
            ):
                raise BusinessRuleError("Заказ не найден.")
            if str(payment.get("external_id") or "") != remote.transaction_id:
                raise VpnPaymentVerificationError(
                    "Platega вернула другой заказ."
                )
            if (
                int(payment["amount_rub"]) != int(remote.amount_rub)
                or str(payment.get("currency") or "") != "RUB"
                or remote.currency != "RUB"
            ):
                raise VpnPaymentVerificationError(
                    "Сумма или валюта платежа не совпадает с заказом."
                )

            if remote.status == PLATEGA_CHARGEBACKED:
                payment, changed = self.payments.mark_provider_status(
                    conn,
                    payment_id=payment_id,
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id=remote.transaction_id,
                    status="refunded",
                )
                referral_reversal = (
                    self.referrals.reverse_vpn_payment_referral_in_transaction(
                        conn,
                        vpn_payment=payment,
                    )
                )
                # Duration is subtracted only on the first paid -> refunded
                # transition. A duplicate callback still verifies the provider
                # transaction but cannot shorten the subscription twice.
                compensation_changed = False
                if changed and payment.get("vpn_subscription_id") is not None:
                    compensation_changed = self._compensate_platega_chargeback(
                        conn,
                        payment=payment,
                    )
                subscription = None
                if payment.get("vpn_subscription_id") is not None:
                    subscription = self.subscriptions.get_by_id(
                        conn, int(payment["vpn_subscription_id"])
                    )
                return VpnPaymentStatusOutcome(
                    payment=payment,
                    subscription=subscription,
                    processed=bool(
                        changed
                        or compensation_changed
                        or referral_reversal.created
                    ),
                    status="refunded",
                )

            recovering_synthetic_failure = (
                payment.get("status") == "failed"
                and remote.status == PLATEGA_CONFIRMED
            )
            existing = (
                None
                if recovering_synthetic_failure
                else self._existing_payment_outcome(conn, payment)
            )
            if existing is not None:
                return existing

            if remote.status == PLATEGA_PENDING:
                return VpnPaymentStatusOutcome(
                    payment=payment,
                    subscription=None,
                    processed=False,
                    status="pending",
                )
            if remote.status == PLATEGA_CANCELED:
                payment, changed = self.payments.mark_provider_status(
                    conn,
                    payment_id=payment_id,
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id=remote.transaction_id,
                    status="cancelled",
                )
                return VpnPaymentStatusOutcome(
                    payment=payment,
                    subscription=None,
                    processed=changed,
                    status="cancelled",
                )
            if remote.status != PLATEGA_CONFIRMED:
                raise VpnPaymentVerificationError(
                    "Platega вернула неизвестный статус."
                )

            plan = self.plans.get_by_id(conn, int(payment["vpn_plan_id"]))
            if plan is None or int(payment["duration_days"]) <= 0:
                raise BusinessRuleError("Параметры заказа повреждены.")
            payment, marked_paid = self.payments.mark_paid(
                conn,
                payment_id=payment_id,
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id=remote.transaction_id,
                user_id=expected_user_id,
            )
            referral_credit = (
                self.referrals.credit_for_vpn_payment_in_transaction(
                    conn,
                    vpn_payment=payment,
                )
            )

            subscription_id = payment.get("vpn_subscription_id")
            if subscription_id is not None:
                subscription = self.subscriptions.get_by_id(
                    conn, int(subscription_id)
                )
                if (
                    subscription is None
                    or int(subscription["user_id"]) != int(payment["user_id"])
                ):
                    raise RuntimeError(
                        "VPN payment points to an invalid subscription"
                    )
                return VpnPaymentStatusOutcome(
                    payment=payment,
                    subscription=subscription,
                    processed=False,
                    status="paid",
                )

            subscription = self._fulfill_paid_payment(
                conn,
                payment=payment,
                plan=plan,
            )
            payment = self.payments.link_subscription(
                conn,
                payment_id=payment_id,
                user_id=int(payment["user_id"]),
                subscription_id=int(subscription["id"]),
                expected_provider=PLATEGA_PROVIDER,
            )
            return VpnPaymentStatusOutcome(
                payment=payment,
                subscription=subscription,
                processed=bool(marked_paid or referral_credit.created),
                status="paid",
            )

    def _existing_payment_outcome(
        self,
        conn: Any,
        payment: Dict[str, Any],
    ) -> VpnPaymentStatusOutcome | None:
        status = str(payment.get("status") or "")
        if status == "pending":
            return None
        if status != "paid":
            return VpnPaymentStatusOutcome(
                payment=payment,
                subscription=None,
                processed=False,
                status=status,
            )
        subscription_id = payment.get("vpn_subscription_id")
        if subscription_id is None:
            # mark-paid and fulfillment are committed in one transaction, so a
            # persisted paid row without a link indicates data corruption.
            raise RuntimeError("Paid VPN payment has no subscription")
        subscription = self.subscriptions.get_by_id(conn, int(subscription_id))
        if (
            subscription is None
            or int(subscription["user_id"]) != int(payment["user_id"])
        ):
            raise RuntimeError("VPN payment points to an invalid subscription")
        return VpnPaymentStatusOutcome(
            payment=payment,
            subscription=subscription,
            processed=False,
            status="paid",
        )

    def _refresh_expired_platega_payment(
        self,
        *,
        client: PlategaClient,
        payment: Dict[str, Any],
        user_id: int,
    ) -> VpnPaymentStatusOutcome:
        external_id = str(payment.get("external_id") or "")
        if not external_id or external_id.startswith("platega_request_"):
            raise BusinessRuleError(
                "Ссылка на оплату ещё создаётся. Попробуйте ещё раз."
            )
        try:
            remote = client.get_transaction(external_id)
        except PlategaError as exc:
            # Never create a replacement while the old transaction could have
            # been paid. A successful authoritative check is required first.
            raise BusinessRuleError(
                "Не удалось проверить старый заказ. Попробуйте ещё раз через минуту."
            ) from exc

        outcome = self._apply_verified_platega_transaction(
            payment_id=int(payment["id"]),
            remote=remote,
            expected_user_id=user_id,
        )
        if outcome.status != "pending":
            return outcome

        # Platega still says PENDING, but its documented checkout lifetime has
        # elapsed. Close the local snapshot so the partial unique index permits
        # a fresh transaction with a usable URL.
        return self._close_expired_platega_payment(
            payment_id=int(payment["id"]),
            external_id=external_id,
            user_id=user_id,
        )

    def _close_expired_platega_payment(
        self,
        *,
        payment_id: int,
        external_id: str,
        user_id: int,
    ) -> VpnPaymentStatusOutcome:
        with self.db.transaction() as conn:
            current = self.payments.get_for_user(conn, payment_id, user_id)
            if current is None:
                raise BusinessRuleError("Заказ не найден.")
            changed = False
            if (
                current.get("status") == "pending"
                and self._platega_payment_link_expired(current)
            ):
                current, changed = self.payments.mark_provider_status(
                    conn,
                    payment_id=payment_id,
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id=external_id,
                    user_id=user_id,
                    status="failed",
                )
            subscription = None
            if current.get("vpn_subscription_id") is not None:
                subscription = self.subscriptions.get_by_id(
                    conn, int(current["vpn_subscription_id"])
                )
            return VpnPaymentStatusOutcome(
                payment=current,
                subscription=subscription,
                processed=changed,
                status=str(current["status"]),
            )

    @classmethod
    def _platega_payment_link_expired(cls, payment: Dict[str, Any]) -> bool:
        expires_at = payment.get("expires_at")
        if expires_at is None or not str(expires_at).strip():
            return True
        try:
            return cls._datetime(expires_at) <= utcnow()
        except (TypeError, ValueError):
            # Corrupt/legacy rows are not safe to reuse. The provider status is
            # still checked before the row is closed.
            return True

    @staticmethod
    def _platega_expires_at(expires_in: str | None) -> str:
        if expires_in is None or not expires_in.strip():
            # Older/partial API responses did not always include expiresIn.
            # A conservative local cap guarantees that such URLs are never
            # reused indefinitely, while the provider is checked before renewal.
            return (utcnow() + _PLATEGA_FALLBACK_LINK_LIFETIME).isoformat()
        match = _PLATEGA_EXPIRES_IN_PATTERN.fullmatch(expires_in.strip())
        if match is None:
            raise VpnPaymentVerificationError(
                "Platega вернула некорректный срок действия платежа."
            )
        lifetime = timedelta(
            hours=int(match.group("hours")),
            minutes=int(match.group("minutes")),
            seconds=int(match.group("seconds")),
        )
        if lifetime <= timedelta(0):
            raise VpnPaymentVerificationError(
                "Platega вернула некорректный срок действия платежа."
            )
        return (utcnow() + lifetime).isoformat()

    def _compensate_platega_chargeback(
        self,
        conn: Any,
        *,
        payment: Dict[str, Any],
    ) -> bool:
        if getattr(conn, "driver", "") == "postgres":
            # Use the same per-user lock as paid renewals. Without it, a
            # concurrent renewal and refund can both derive a new ends_at from
            # the same old value and the last writer loses either the paid
            # extension or the chargeback subtraction.
            locked_user = conn.execute(
                "SELECT id FROM users WHERE id = ? FOR UPDATE",
                (int(payment["user_id"]),),
            ).fetchone()
            if locked_user is None:
                raise RuntimeError("VPN payment user is missing")
        subscription_id = int(payment["vpn_subscription_id"])
        payment_id = int(payment["id"])
        subscription = self.subscriptions.get_by_id(conn, subscription_id)
        if (
            subscription is None
            or int(subscription["user_id"]) != int(payment["user_id"])
        ):
            raise RuntimeError("VPN payment points to an invalid subscription")

        superseded = self.jobs.supersede_payment_jobs(
            conn,
            subscription_id=subscription_id,
            payment_id=payment_id,
            reason=f"Superseded by chargeback for VPN payment {payment_id}",
        )
        remaining_payment = self.payments.get_latest_other_paid_for_subscription(
            conn,
            subscription_id=subscription_id,
            excluding_payment_id=payment_id,
        )
        duration = timedelta(days=int(payment["duration_days"]))
        shortened_end = self._datetime(subscription["ends_at"]) - duration

        # A shared subscription displays the latest still-paid plan. When a
        # paid extension of a trial is fully reversed, return it to a plan-less
        # trial. A standalone paid subscription keeps its historical plan while
        # disabled so support can still identify what was originally bought.
        if remaining_payment is not None:
            plan_id: int | None = int(remaining_payment["vpn_plan_id"])
        elif str(subscription["kind"]) == "trial":
            plan_id = None
        else:
            current_plan_id = subscription.get("plan_id")
            plan_id = int(current_plan_id) if current_plan_id is not None else None

        shortened = self.subscriptions.update_period(
            conn,
            subscription_id=subscription_id,
            plan_id=plan_id,
            kind=str(subscription["kind"]),
            starts_at=self._iso_value(subscription["starts_at"]),
            ends_at=shortened_end.isoformat(),
            status=str(subscription["status"]),
        )

        if shortened_end <= utcnow():
            self.subscriptions.mark_status(
                conn,
                subscription_id=subscription_id,
                status="disabled",
                last_error=(
                    f"Access revoked: chargeback for VPN payment {payment_id}"
                ),
            )
            _, job_created = self.jobs.enqueue(
                conn,
                subscription_id=subscription_id,
                operation="disable",
                idempotency_key=f"vpn:chargeback:{payment_id}:disable",
            )
        else:
            # Marzban must receive the shortened expire value even when another
            # paid order or the original trial still owns valid entitlement.
            _, job_created = self.jobs.enqueue(
                conn,
                subscription_id=subscription_id,
                operation="update",
                idempotency_key=f"vpn:chargeback:{payment_id}:update",
            )

        return bool(superseded or shortened or job_created)

    def _require_platega(self) -> PlategaClient:
        if self.payment_provider != PLATEGA_PROVIDER:
            raise BusinessRuleError("Оплата Platega временно отключена.")
        if self.platega_client is None:
            raise BusinessRuleError("Оплата Platega ещё не настроена.")
        return self.platega_client

    def _require_checkout_ready_server(self, conn: Any) -> Dict[str, Any]:
        healthy_after = (
            utcnow() - timedelta(seconds=self.worker_health_max_age_seconds)
        ).isoformat()
        server = self.servers.get_checkout_ready_by_code(
            conn,
            code=self.server_code,
            healthy_after=healthy_after,
        )
        if server is None:
            raise BusinessRuleError(
                "VPN-сервер сейчас недоступен. Попробуйте ещё раз чуть позже."
            )
        return server

    def _public_url(self, path: str) -> str:
        return f"{self.app_base_url}{self._normalize_path(path)}"

    @staticmethod
    def _normalize_path(path: str) -> str:
        cleaned = path.strip()
        return cleaned if cleaned.startswith("/") else f"/{cleaned}"

    def get_current_subscription(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.subscriptions.get_latest_for_user(conn, user_id)

    def _fulfill_paid_payment(
        self,
        conn: Any,
        *,
        payment: Dict[str, Any],
        plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        if payment.get("status") != "paid":
            raise BusinessRuleError("Оплата заказа не подтверждена.")

        user_id = int(payment["user_id"])
        payment_id = int(payment["id"])
        if getattr(conn, "driver", "") == "postgres":
            # Serialize separate paid orders for the same user so concurrent
            # renewals cannot overwrite each other's added duration.
            locked_user = conn.execute(
                "SELECT id FROM users WHERE id = ? FOR UPDATE",
                (user_id,),
            ).fetchone()
            if locked_user is None:
                raise RuntimeError("VPN payment user is missing")
        now = utcnow()
        duration = timedelta(days=int(payment["duration_days"]))
        live = self.subscriptions.get_live_for_user(conn, user_id)
        if live is not None:
            current_end = self._datetime(live["ends_at"])
            ends_at = max(now, current_end) + duration
            subscription = self.subscriptions.update_period(
                conn,
                subscription_id=int(live["id"]),
                plan_id=int(plan["id"]),
                kind=str(live["kind"]),
                starts_at=self._iso_value(live["starts_at"]),
                ends_at=ends_at.isoformat(),
                status=(
                    "active" if live["status"] == "active" else "provisioning"
                ),
            )
            operation = "update"
        else:
            server = self.servers.get_by_code(conn, self.server_code)
            if server is None or not bool(server["is_active"]):
                raise BusinessRuleError(
                    "Сервер сейчас готовится. Попробуйте ещё раз чуть позже."
                )
            subscription = self.subscriptions.create_provisioning(
                conn,
                user_id=user_id,
                server_id=int(server["id"]),
                plan_id=int(plan["id"]),
                kind="paid",
                provider_username=f"u_{secrets.token_hex(12)}",
                starts_at=now.isoformat(),
                ends_at=(now + duration).isoformat(),
            )
            operation = "create"

        self.jobs.enqueue(
            conn,
            subscription_id=int(subscription["id"]),
            operation=operation,
            idempotency_key=f"vpn:payment:{payment_id}:{operation}",
        )
        return subscription

    def _require_admin_demo_access(self, admin_authorized: bool) -> None:
        if not self.allow_admin_demo_payment or not admin_authorized:
            raise BusinessRuleError(
                "Тестовая оплата доступна только владельцу бота."
            )

    def enqueue_due_expirations(self, *, limit: int = 100) -> int:
        queued = 0
        with self.db.transaction() as conn:
            due = self.subscriptions.list_due_for_expiration(conn, limit=limit)
            for subscription in due:
                subscription_id = int(subscription["id"])
                if subscription["status"] == "active":
                    self.subscriptions.mark_status(
                        conn,
                        subscription_id=subscription_id,
                        status="expired",
                    )
                _, created = self.jobs.enqueue(
                    conn,
                    subscription_id=subscription_id,
                    operation="disable",
                    idempotency_key=(
                        f"vpn:disable:{subscription_id}:"
                        f"{self._iso_value(subscription['ends_at'])}"
                    ),
                )
                queued += int(created)
        return queued

    def claim_worker_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        control_plane_ready: bool = False,
        worker_inbound_tags: Any = None,
    ) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            server = self.servers.get_by_worker_id(conn, worker_id)
            if server is None or not bool(server["is_active"]):
                raise BusinessRuleError("Unknown or inactive VPN worker")
            if control_plane_ready is not True:
                raise BusinessRuleError("VPN worker control plane is not ready")
            profile_v2_ready = (
                isinstance(worker_inbound_tags, list)
                and tuple(worker_inbound_tags) == _MARZBAN_VLESS_INBOUND_TAGS
            )

            # Only a signed claim made after the worker successfully queried
            # its loopback Marzban API may renew checkout readiness.
            self.servers.mark_healthy(conn, server_id=int(server["id"]))

            current = utcnow().isoformat()
            due = self.subscriptions.list_due_for_expiration(
                conn,
                due_at=current,
                limit=100,
            )
            for subscription in due:
                if int(subscription["server_id"]) != int(server["id"]):
                    continue
                subscription_id = int(subscription["id"])
                if subscription["status"] == "active":
                    self.subscriptions.mark_status(
                        conn,
                        subscription_id=subscription_id,
                        status="expired",
                    )
                self.jobs.enqueue(
                    conn,
                    subscription_id=subscription_id,
                    operation="disable",
                    idempotency_key=(
                        f"vpn:disable:{subscription_id}:"
                        f"{self._iso_value(subscription['ends_at'])}"
                    ),
                )

            job = self.jobs.claim_due(
                conn,
                lease_seconds=lease_seconds,
                server_id=int(server["id"]),
                excluded_idempotency_prefix=(
                    None if profile_v2_ready else "vpn:profile:v2:"
                ),
            )
            if job is None and profile_v2_ready:
                # Migrate one existing active account at a time only while the
                # normal provisioning queue is idle. The fixed profile-version
                # key makes this safe on every worker poll and across restarts.
                profile_updates = (
                    self.subscriptions.list_active_requiring_profile_update(
                        conn,
                        server_id=int(server["id"]),
                        profile_version=_MARZBAN_PROFILE_VERSION,
                        active_at=current,
                        limit=1,
                    )
                )
                for subscription in profile_updates:
                    subscription_id = int(subscription["id"])
                    self.jobs.enqueue(
                        conn,
                        subscription_id=subscription_id,
                        operation="update",
                        idempotency_key=(
                            f"vpn:profile:{_MARZBAN_PROFILE_VERSION}:"
                            f"{subscription_id}"
                        ),
                    )
                if profile_updates:
                    job = self.jobs.claim_due(
                        conn,
                        lease_seconds=lease_seconds,
                        server_id=int(server["id"]),
                    )
            if job is None:
                return None

            subscription = self.subscriptions.get_by_id(
                conn, int(job["subscription_id"])
            )
            if subscription is None:
                raise RuntimeError("VPN job points to a missing subscription")

            operation = str(job["operation"])
            desired_status = "disabled" if operation == "disable" else "active"
            payload: Dict[str, Any] = {
                "username": subscription["provider_username"],
                "status": desired_status,
            }
            if operation != "disable":
                payload.update(
                    {
                        "proxies": {
                            "vless": {"flow": "xtls-rprx-vision"},
                        },
                        "inbounds": {
                            "vless": list(_MARZBAN_VLESS_INBOUND_TAGS),
                        },
                        "expire": int(self._datetime(subscription["ends_at"]).timestamp()),
                        "data_limit": 0,
                        "data_limit_reset_strategy": "no_reset",
                        "note": f"CEA VPN subscription {subscription['id']}",
                    }
                )

            return {
                "job_id": int(job["id"]),
                "lease_token": job["lease_token"],
                "operation": operation,
                "attempt": int(job["attempts"]),
                "marzban_user": payload,
                "subscription_base_url": server["subscription_base_url"],
            }

    def complete_worker_job(
        self,
        *,
        worker_id: str,
        job_id: int,
        lease_token: str,
        subscription_url: str = "",
    ) -> VpnJobCompletion:
        with self.db.transaction() as conn:
            server = self._require_worker_server(conn, worker_id)
            job = self._require_worker_job(
                conn,
                server_id=int(server["id"]),
                job_id=job_id,
                lease_token=lease_token,
            )
            subscription_id = int(job["subscription_id"])
            operation = str(job["operation"])

            if operation == "disable":
                subscription = self.subscriptions.mark_status(
                    conn,
                    subscription_id=subscription_id,
                    status="disabled",
                )
            else:
                self._validate_subscription_url(
                    subscription_url,
                    str(server["subscription_base_url"]),
                )
                subscription = self.subscriptions.mark_active(
                    conn,
                    subscription_id=subscription_id,
                    subscription_url=subscription_url,
                )
                if subscription["kind"] == "trial":
                    claim = self.trials.get_by_subscription_id(conn, subscription_id)
                    if claim is not None:
                        self.trials.mark_status(
                            conn,
                            claim_id=int(claim["id"]),
                            status="provisioned",
                        )

            self.jobs.mark_completed(
                conn,
                job_id=job_id,
                lease_token=lease_token,
            )
            self.servers.mark_healthy(conn, server_id=int(server["id"]))
            user = conn.execute(
                "SELECT telegram_id FROM users WHERE id = ?",
                (int(subscription["user_id"]),),
            ).fetchone()
            if user is None:
                raise RuntimeError("VPN subscription user is missing")
            completion_subscription = subscription
            if operation == "update" and str(
                job.get("idempotency_key") or ""
            ).startswith(("vpn:chargeback:", "vpn:profile:")):
                # `notify_vpn_ready` deliberately ignores completions without a
                # URL. Keep the real URL in the database/Marzban, but suppress a
                # misleading second "VPN готов" message for this purely
                # technical profile or expiry correction.
                completion_subscription = dict(subscription)
                completion_subscription["subscription_url"] = ""
            return VpnJobCompletion(
                subscription=completion_subscription,
                telegram_id=int(user["telegram_id"]),
                operation=operation,
            )

    def fail_worker_job(
        self,
        *,
        worker_id: str,
        job_id: int,
        lease_token: str,
        error_message: str,
    ) -> None:
        with self.db.transaction() as conn:
            server = self._require_worker_server(conn, worker_id)
            job = self._require_worker_job(
                conn,
                server_id=int(server["id"]),
                job_id=job_id,
                lease_token=lease_token,
            )
            attempts = max(1, int(job["attempts"]))
            delay_seconds = min(900, 5 * (2 ** min(attempts - 1, 8)))
            next_attempt = utcnow() + timedelta(seconds=delay_seconds)
            clean_error = " ".join(error_message.split())[:500] or "worker failure"
            self.jobs.mark_failed(
                conn,
                job_id=job_id,
                lease_token=lease_token,
                error_message=clean_error,
                next_attempt_at=next_attempt.isoformat(),
            )
            is_profile_convergence = str(
                job.get("idempotency_key") or ""
            ).startswith("vpn:profile:")
            if (
                attempts >= 5
                and str(job["operation"]) != "disable"
                and not is_profile_convergence
            ):
                self.subscriptions.mark_status(
                    conn,
                    subscription_id=int(job["subscription_id"]),
                    status="error",
                    last_error="Provisioning is being retried",
                )

    def _require_worker_server(self, conn: Any, worker_id: str) -> Dict[str, Any]:
        server = self.servers.get_by_worker_id(conn, worker_id)
        if server is None:
            raise BusinessRuleError("Unknown VPN worker")
        return server

    def _require_worker_job(
        self,
        conn: Any,
        *,
        server_id: int,
        job_id: int,
        lease_token: str,
    ) -> Dict[str, Any]:
        job = self.jobs.get_by_id(conn, job_id)
        if (
            job is None
            or job["status"] != "running"
            or not secrets.compare_digest(str(job.get("lease_token") or ""), lease_token)
        ):
            raise BusinessRuleError("VPN worker lease is no longer valid")
        subscription = self.subscriptions.get_by_id(
            conn, int(job["subscription_id"])
        )
        if subscription is None or int(subscription["server_id"]) != server_id:
            raise BusinessRuleError("VPN job belongs to another worker")
        return job

    @staticmethod
    def _validate_subscription_url(value: str, base_url: str) -> None:
        base = base_url.strip().rstrip("/")
        candidate = value.strip()
        base_parts = urlsplit(base)
        candidate_parts = urlsplit(candidate)
        if (
            base_parts.scheme != "https"
            or not base_parts.netloc
            or candidate_parts.scheme != "https"
            or candidate_parts.netloc != base_parts.netloc
            or not candidate.startswith(f"{base}/sub/")
        ):
            raise BusinessRuleError("Worker returned an invalid subscription URL")

    @staticmethod
    def _datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_iso(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _iso_value(cls, value: Any) -> str:
        return cls._datetime(value).isoformat()


__all__ = [
    "VpnJobCompletion",
    "VpnPaymentOutcome",
    "VpnPaymentStatusOutcome",
    "VpnPaymentVerificationError",
    "VpnService",
    "VpnTrialOutcome",
]
