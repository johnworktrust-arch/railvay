from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict

from ceai.database import Database
from ceai.repositories.referrals import ReferralRepository
from ceai.repositories.users import UserRepository


REFERRAL_RATE_PERCENT = 30
REFERRAL_WITHDRAWAL_MIN_KOPECKS = 100_000


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
    ) -> bool:
        referral_code = self.referral_code_from_start_text(start_text)
        if not referral_code:
            return False
        with self.db.transaction() as conn:
            return self.apply_referral_code(conn, user_id=user_id, referral_code=referral_code)

    def apply_referral_code(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        referral_code: str,
    ) -> bool:
        user = self.users.get_by_id(conn, user_id)
        if user is None or user.get("referred_by_user_id"):
            return False

        referrer = self.referrals.get_user_by_code(conn, referral_code)
        if referrer is None or int(referrer["id"]) == int(user_id):
            return False

        return self.referrals.assign_referrer(
            conn,
            user_id=user_id,
            referrer_user_id=int(referrer["id"]),
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
