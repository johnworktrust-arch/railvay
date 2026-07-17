from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.referrals import ReferralRepository
from ceai.repositories.users import UserRepository


REFERRAL_RATE_PERCENT = 30
REFERRAL_WITHDRAWAL_MIN_KOPECKS = 50_000


@dataclass(frozen=True)
class ReferralStats:
    invited_count: int
    balance_kopecks: int
    withdrawal_method: str
    requisites: str
    rate_percent: int = REFERRAL_RATE_PERCENT
    withdrawal_min_kopecks: int = REFERRAL_WITHDRAWAL_MIN_KOPECKS


@dataclass(frozen=True)
class ReferralCreditResult:
    transaction: Dict[str, Any] | None
    created: bool
    amount_kopecks: int


@dataclass(frozen=True)
class ReferralApplyResult:
    assigned: bool
    already_registered: bool = False
    referrer_user_id: int | None = None
    referrer_telegram_id: int | None = None
    referred_user_id: int | None = None
    referred_telegram_id: int | None = None

    def __bool__(self) -> bool:
        return self.assigned


class ReferralService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.referrals = ReferralRepository()
        self.users = UserRepository()

    @staticmethod
    def referral_code_from_start_text(text: str | None) -> str | None:
        raw = (text or "").strip()
        if not raw:
            return None
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return None
        payload = parts[1].strip()
        if not payload:
            return None
        for prefix in ("ref_", "ref-"):
            if payload.startswith(prefix):
                payload = payload[len(prefix) :]
                break
        return payload.strip() or None

    def apply_start_referral(
        self,
        *,
        user_id: int,
        start_text: str | None,
        user_was_registered: bool = False,
    ) -> ReferralApplyResult:
        referral_code = self.referral_code_from_start_text(start_text)
        if not referral_code:
            return ReferralApplyResult(assigned=False)
        if user_was_registered:
            return ReferralApplyResult(assigned=False, already_registered=True)
        with self.db.transaction() as conn:
            return self.apply_referral_code(conn, user_id=user_id, referral_code=referral_code)

    def apply_referral_code(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        referral_code: str,
    ) -> ReferralApplyResult:
        user = self.users.get_by_id(conn, user_id)
        if user is None or user.get("referred_by_user_id"):
            return ReferralApplyResult(assigned=False)

        referrer = self.referrals.get_user_by_code(conn, referral_code)
        if referrer is None or int(referrer["id"]) == int(user_id):
            return ReferralApplyResult(assigned=False)

        assigned = self.referrals.assign_referrer(
            conn,
            user_id=user_id,
            referrer_user_id=int(referrer["id"]),
        )
        if not assigned:
            return ReferralApplyResult(assigned=False)
        return ReferralApplyResult(
            assigned=True,
            referrer_user_id=int(referrer["id"]),
            referrer_telegram_id=int(referrer["telegram_id"]),
            referred_user_id=int(user["id"]),
            referred_telegram_id=int(user["telegram_id"]),
        )

    def credit_for_payment_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        payment: Dict[str, Any],
    ) -> ReferralCreditResult:
        payer = self.users.get_by_id(conn, int(payment["user_id"]))
        if payer is None:
            return ReferralCreditResult(transaction=None, created=False, amount_kopecks=0)

        referrer_user_id = payer.get("referred_by_user_id")
        if not referrer_user_id:
            return ReferralCreditResult(transaction=None, created=False, amount_kopecks=0)

        net_amount_rub = max(
            int(payment.get("amount_rub") or 0) - int(payment.get("discount_rub") or 0),
            0,
        )
        amount_kopecks = net_amount_rub * REFERRAL_RATE_PERCENT
        if amount_kopecks <= 0:
            return ReferralCreditResult(transaction=None, created=False, amount_kopecks=0)

        transaction, created = self.referrals.create_credit(
            conn,
            referrer_user_id=int(referrer_user_id),
            referred_user_id=int(payment["user_id"]),
            payment_id=int(payment["id"]),
            amount_kopecks=amount_kopecks,
            rate_percent=REFERRAL_RATE_PERCENT,
            idempotency_key=f"referral:payment:{payment['id']}:credit",
        )
        return ReferralCreditResult(
            transaction=transaction,
            created=created,
            amount_kopecks=int(transaction["amount_kopecks"]),
        )

    def credit_for_vpn_payment_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment: Dict[str, Any],
    ) -> ReferralCreditResult:
        source = self._vpn_payment_source(vpn_payment)
        if source is None:
            return self._empty_credit_result()
        vpn_payment_id, provider, external_id = source
        canonical = self.referrals.get_paid_vpn_payment(
            conn,
            vpn_payment_id=vpn_payment_id,
            provider=provider,
            external_id=external_id,
        )
        if canonical is None or canonical.get("currency") != "RUB":
            return self._empty_credit_result()

        payer = self.users.get_by_id(conn, int(canonical["user_id"]))
        if payer is None:
            return self._empty_credit_result()
        referrer_user_id = payer.get("referred_by_user_id")
        if not referrer_user_id or int(referrer_user_id) == int(payer["id"]):
            return self._empty_credit_result()

        amount_kopecks = (
            int(canonical.get("amount_rub") or 0)
            * 100
            * REFERRAL_RATE_PERCENT
            // 100
        )
        if amount_kopecks <= 0:
            return self._empty_credit_result()

        transaction, created = self.referrals.create_vpn_credit(
            conn,
            referrer_user_id=int(referrer_user_id),
            referred_user_id=int(canonical["user_id"]),
            vpn_payment_provider=provider,
            vpn_payment_id=vpn_payment_id,
            vpn_payment_external_id=external_id,
            amount_kopecks=amount_kopecks,
            rate_percent=REFERRAL_RATE_PERCENT,
            idempotency_key=(
                f"referral:vpn_payment:{provider}:{vpn_payment_id}:credit"
            ),
        )
        return ReferralCreditResult(
            transaction=transaction,
            created=created,
            amount_kopecks=int(transaction["amount_kopecks"]),
        )

    def reverse_vpn_payment_referral_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        vpn_payment: Dict[str, Any],
    ) -> ReferralCreditResult:
        source = self._vpn_payment_source(vpn_payment)
        if source is None:
            return self._empty_credit_result()
        vpn_payment_id, provider, external_id = source
        canonical = self.referrals.get_refunded_vpn_payment(
            conn,
            vpn_payment_id=vpn_payment_id,
            provider=provider,
            external_id=external_id,
        )
        if canonical is None:
            return self._empty_credit_result()

        credit = self.referrals.get_vpn_source_transaction(
            conn,
            vpn_payment_provider=provider,
            vpn_payment_id=vpn_payment_id,
            transaction_type="credit",
        )
        if credit is None:
            return self._empty_credit_result()
        transaction, created = self.referrals.create_vpn_chargeback_adjustment(
            conn,
            credit_transaction=credit,
            idempotency_key=(
                f"referral:vpn_payment:{provider}:{vpn_payment_id}:chargeback"
            ),
        )
        return ReferralCreditResult(
            transaction=transaction,
            created=created,
            amount_kopecks=int(transaction["amount_kopecks"]),
        )

    @staticmethod
    def _vpn_payment_source(
        vpn_payment: Dict[str, Any],
    ) -> tuple[int, str, str] | None:
        try:
            vpn_payment_id = int(vpn_payment["id"])
        except (KeyError, TypeError, ValueError):
            return None
        raw_provider = vpn_payment.get("provider")
        raw_external_id = vpn_payment.get("external_id")
        if not isinstance(raw_provider, str) or not isinstance(
            raw_external_id, str
        ):
            return None
        provider = raw_provider.strip().lower()
        external_id = raw_external_id.strip()
        if vpn_payment_id <= 0 or not provider or not external_id:
            return None
        return vpn_payment_id, provider, external_id

    @staticmethod
    def _empty_credit_result() -> ReferralCreditResult:
        return ReferralCreditResult(
            transaction=None,
            created=False,
            amount_kopecks=0,
        )

    def stats(self, user_id: int) -> ReferralStats:
        with self.db.transaction() as conn:
            settings = self.referrals.get_payout_settings(conn, user_id) or {}
            return ReferralStats(
                invited_count=self.referrals.invited_count(conn, user_id),
                balance_kopecks=self.referrals.balance_kopecks(conn, user_id),
                withdrawal_method=str(settings.get("withdrawal_method") or ""),
                requisites=str(settings.get("requisites") or ""),
            )

    def set_payout_settings(
        self,
        *,
        user_id: int,
        withdrawal_method: str,
        requisites: str,
    ) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            return self.referrals.upsert_payout_settings(
                conn,
                user_id=user_id,
                withdrawal_method=withdrawal_method.strip(),
                requisites=requisites.strip(),
            )


def format_rubles_from_kopecks(amount_kopecks: int) -> str:
    sign = "-" if amount_kopecks < 0 else ""
    absolute = abs(amount_kopecks)
    rubles = absolute // 100
    kopecks = absolute % 100
    if kopecks == 0:
        return f"{sign}{rubles} ₽"
    return f"{sign}{rubles}.{kopecks:02d} ₽"
