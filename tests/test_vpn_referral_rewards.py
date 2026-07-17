from __future__ import annotations

import unittest
from pathlib import Path

from ceai.database import Database
from ceai.repositories.vpn_payments import (
    ADMIN_DEMO_PROVIDER,
    VpnPaymentRepository,
)
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.services.referrals import ReferralService
from ceai.services.users import UserService


class VpnReferralRewardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        users = UserService(self.db)
        self.referrer = users.ensure_telegram_user(
            telegram_id=77101,
            username="vpn_referrer",
            first_name="Referrer",
            language_code="ru",
        )
        self.buyer = users.ensure_telegram_user(
            telegram_id=77102,
            username="vpn_buyer",
            first_name="Buyer",
            language_code="ru",
        )
        self.referrals = ReferralService(self.db)
        applied = self.referrals.apply_start_referral(
            user_id=int(self.buyer["id"]),
            start_text=f"/start ref_{self.referrer['referral_code']}",
        )
        self.assertTrue(applied.assigned)
        with self.db.transaction() as conn:
            self.plan = VpnPlanRepository().upsert(
                conn,
                code="vpn-referral-1m",
                name="1 month",
                duration_days=30,
                price_rub=189,
                price_stars=149,
                max_devices=3,
            )
        self.payments = VpnPaymentRepository()

    def tearDown(self) -> None:
        self.db.close()

    def _pending_payment(self, *, user_id: int | None = None) -> dict:
        with self.db.transaction() as conn:
            payment, created = self.payments.create_or_get_pending_admin_demo(
                conn,
                user_id=user_id or int(self.buyer["id"]),
                plan_id=int(self.plan["id"]),
                amount_rub=189,
                duration_days=30,
                payment_method="sbp",
            )
        self.assertTrue(created)
        return payment

    def _mark_paid(self, payment: dict) -> dict:
        with self.db.transaction() as conn:
            paid, changed = self.payments.mark_paid(
                conn,
                payment_id=int(payment["id"]),
                expected_provider=ADMIN_DEMO_PROVIDER,
                expected_external_id=str(payment["external_id"]),
                user_id=int(payment["user_id"]),
            )
        self.assertTrue(changed)
        return paid

    def test_paid_vpn_reward_is_30_percent_and_idempotent(self) -> None:
        paid = self._mark_paid(self._pending_payment())

        with self.db.transaction() as conn:
            first = self.referrals.credit_for_vpn_payment_in_transaction(
                conn, vpn_payment=paid
            )
            duplicate = self.referrals.credit_for_vpn_payment_in_transaction(
                conn, vpn_payment=paid
            )
            rows = conn.execute(
                """
                SELECT * FROM referral_transactions
                WHERE vpn_payment_provider = ? AND vpn_payment_id = ?
                ORDER BY id
                """,
                (ADMIN_DEMO_PROVIDER, int(paid["id"])),
            ).fetchall()

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.amount_kopecks, 5_670)
        self.assertEqual(duplicate.amount_kopecks, 5_670)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["payment_id"])
        self.assertEqual(rows[0]["type"], "credit")
        self.assertEqual(rows[0]["vpn_payment_provider"], ADMIN_DEMO_PROVIDER)
        self.assertEqual(rows[0]["vpn_payment_id"], paid["id"])
        self.assertEqual(
            rows[0]["vpn_payment_external_id"], paid["external_id"]
        )
        self.assertEqual(
            self.referrals.stats(int(self.referrer["id"])).balance_kopecks,
            5_670,
        )

    def test_pending_or_forged_payment_never_credits(self) -> None:
        pending = self._pending_payment()
        forged = {**pending, "status": "paid"}

        with self.db.transaction() as conn:
            pending_result = (
                self.referrals.credit_for_vpn_payment_in_transaction(
                    conn, vpn_payment=forged
                )
            )
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM referral_transactions"
            ).fetchone()["count"]

        self.assertFalse(pending_result.created)
        self.assertEqual(pending_result.amount_kopecks, 0)
        self.assertEqual(count, 0)

        paid = self._mark_paid(pending)
        mismatched = {**paid, "external_id": "not-the-provider-order"}
        with self.db.transaction() as conn:
            mismatch_result = (
                self.referrals.credit_for_vpn_payment_in_transaction(
                    conn, vpn_payment=mismatched
                )
            )
        self.assertFalse(mismatch_result.created)
        self.assertEqual(mismatch_result.amount_kopecks, 0)

    def test_self_referral_is_defensively_rejected(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET referred_by_user_id = id WHERE id = ?",
                (int(self.buyer["id"]),),
            )
        paid = self._mark_paid(self._pending_payment())

        with self.db.transaction() as conn:
            result = self.referrals.credit_for_vpn_payment_in_transaction(
                conn, vpn_payment=paid
            )

        self.assertFalse(result.created)
        self.assertEqual(result.amount_kopecks, 0)
        self.assertEqual(
            self.referrals.stats(int(self.buyer["id"])).balance_kopecks,
            0,
        )

    def test_chargeback_reverses_reward_once_and_restores_balance(self) -> None:
        paid = self._mark_paid(self._pending_payment())
        with self.db.transaction() as conn:
            credited = self.referrals.credit_for_vpn_payment_in_transaction(
                conn, vpn_payment=paid
            )
        self.assertEqual(credited.amount_kopecks, 5_670)

        with self.db.transaction() as conn:
            refunded, changed = self.payments.mark_provider_status(
                conn,
                payment_id=int(paid["id"]),
                expected_provider=ADMIN_DEMO_PROVIDER,
                expected_external_id=str(paid["external_id"]),
                status="refunded",
            )
            first = self.referrals.reverse_vpn_payment_referral_in_transaction(
                conn, vpn_payment=refunded
            )
            duplicate = (
                self.referrals.reverse_vpn_payment_referral_in_transaction(
                    conn, vpn_payment=refunded
                )
            )
            rows = conn.execute(
                """
                SELECT type, amount_kopecks, reason
                FROM referral_transactions
                WHERE vpn_payment_provider = ? AND vpn_payment_id = ?
                ORDER BY id
                """,
                (ADMIN_DEMO_PROVIDER, int(paid["id"])),
            ).fetchall()

        self.assertTrue(changed)
        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.amount_kopecks, -5_670)
        self.assertEqual(duplicate.amount_kopecks, -5_670)
        self.assertEqual(
            [(row["type"], row["amount_kopecks"]) for row in rows],
            [("credit", 5_670), ("adjustment", -5_670)],
        )
        self.assertEqual(rows[1]["reason"], "vpn_payment_referral_chargeback")
        self.assertEqual(
            self.referrals.stats(int(self.referrer["id"])).balance_kopecks,
            0,
        )

    def test_chargeback_without_credit_is_a_safe_noop(self) -> None:
        paid = self._mark_paid(self._pending_payment())
        with self.db.transaction() as conn:
            refunded, _ = self.payments.mark_provider_status(
                conn,
                payment_id=int(paid["id"]),
                expected_provider=ADMIN_DEMO_PROVIDER,
                expected_external_id=str(paid["external_id"]),
                status="refunded",
            )
            result = self.referrals.reverse_vpn_payment_referral_in_transaction(
                conn, vpn_payment=refunded
            )

        self.assertFalse(result.created)
        self.assertEqual(result.amount_kopecks, 0)

    def test_migrations_have_no_cross_product_payment_foreign_key(self) -> None:
        for path in (
            Path("migrations/012_vpn_referral_rewards.sql"),
            Path("migrations/postgres/012_vpn_referral_rewards.sql"),
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn("vpn_payment_provider", source)
            self.assertIn("vpn_payment_id", source)
            self.assertIn("idx_referral_tx_vpn_payment_type", source)
            self.assertNotIn("REFERENCES payments", source)


if __name__ == "__main__":
    unittest.main()
