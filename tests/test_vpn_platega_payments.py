from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from ceai.database import Database
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.services.exceptions import BusinessRuleError
from ceai.services.platega import (
    PLATEGA_CANCELED,
    PLATEGA_CHARGEBACKED,
    PLATEGA_CONFIRMED,
    PLATEGA_PENDING,
    PlategaCallbackAuthenticationError,
    PlategaCreatedPayment,
    PlategaRequestError,
    PlategaTransaction,
)
from ceai.services.referrals import ReferralService
from ceai.services.users import UserService
from ceai.services.vpn import VpnPaymentVerificationError, VpnService
from ceai.time_utils import parse_iso, utcnow


class FakePlategaClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.transactions: dict[str, PlategaTransaction] = {}
        self.get_calls: list[str] = []
        self.expires_in: str | None = "00:15:00"

    def create_payment(self, **kwargs) -> PlategaCreatedPayment:
        transaction_id = str(uuid.uuid4())
        self.created.append(dict(kwargs))
        self.transactions[transaction_id] = PlategaTransaction(
            transaction_id=transaction_id,
            status=PLATEGA_PENDING,
            amount_rub=int(kwargs["amount_rub"]),
            currency="RUB",
            payment_method=None,
        )
        return PlategaCreatedPayment(
            transaction_id=transaction_id,
            status=PLATEGA_PENDING,
            payment_url=f"https://pay.platega.io/{transaction_id}",
            expires_in=self.expires_in,
        )

    def get_transaction(self, transaction_id: str) -> PlategaTransaction:
        self.get_calls.append(transaction_id)
        return self.transactions[transaction_id]

    def authenticate_callback(self, headers) -> None:
        if (
            headers.get("X-MerchantId") != "merchant"
            or headers.get("X-Secret") != "secret"
        ):
            raise PlategaCallbackAuthenticationError("invalid callback")

    def set_status(
        self,
        transaction_id: str,
        status: str,
        *,
        amount_rub: int | None = None,
        currency: str = "RUB",
    ) -> None:
        current = self.transactions[transaction_id]
        self.transactions[transaction_id] = PlategaTransaction(
            transaction_id=transaction_id,
            status=status,
            amount_rub=(
                current.amount_rub if amount_rub is None else amount_rub
            ),
            currency=currency,
            payment_method=2,
        )


class VpnPlategaPaymentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        users = UserService(self.db)
        self.user = users.ensure_telegram_user(
            telegram_id=88101,
            username="platega_user",
            first_name="Paying",
            last_name="User",
            language_code="ru",
        )
        self.other = users.ensure_telegram_user(
            telegram_id=88102,
            username="other_user",
            first_name="Other",
            last_name="User",
            language_code="ru",
        )
        with self.db.transaction() as conn:
            servers = VpnServerRepository()
            server = servers.upsert(
                conn,
                code="nl-1",
                name="Amsterdam",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl1",
                subscription_base_url="https://sub.example.test",
            )
            servers.mark_healthy(
                conn,
                server_id=int(server["id"]),
                checked_at=utcnow().isoformat(),
            )
            VpnPlanRepository().upsert(
                conn,
                code="vpn-1m",
                name="1 месяц",
                duration_days=30,
                price_rub=189,
                price_stars=149,
                max_devices=3,
            )
            VpnPlanRepository().upsert(
                conn,
                code="vpn-3m",
                name="3 месяца",
                duration_days=90,
                price_rub=399,
                price_stars=349,
                max_devices=3,
            )
        self.client = FakePlategaClient()
        self.vpn = VpnService(
            self.db,
            server_code="nl-1",
            payment_provider="platega",
            app_base_url="https://bot.example.test",
            platega_client=self.client,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.db.close()

    def _create(self, *, plan_code: str = "vpn-1m") -> dict:
        return self.vpn.create_platega_payment(
            user_id=int(self.user["id"]),
            plan_code=plan_code,
            user_name="platega_user",
        )[0]

    def _counts(self) -> tuple[int, int]:
        with self.db.transaction() as conn:
            subscriptions = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_subscriptions"
            ).fetchone()["count"]
            jobs = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_provisioning_jobs"
            ).fetchone()["count"]
        return int(subscriptions), int(jobs)

    def test_create_uses_universal_payment_page_but_issues_no_vpn(self) -> None:
        order = self._create()

        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["provider"], "platega")
        self.assertTrue(str(order["payment_url"]).startswith(
            "https://pay.platega.io/"
        ))
        expires_at = parse_iso(str(order["expires_at"]))
        self.assertGreater(expires_at, utcnow() + timedelta(minutes=14))
        self.assertLess(expires_at, utcnow() + timedelta(minutes=16))
        self.assertEqual(self._counts(), (0, 0))
        request = self.client.created[0]
        self.assertEqual(request["amount_rub"], 189)
        self.assertEqual(request["return_url"], (
            "https://bot.example.test/payments/vpn/platega/return"
        ))
        self.assertEqual(request["failed_url"], (
            "https://bot.example.test/payments/vpn/platega/failed"
        ))

    def test_checkout_is_blocked_when_worker_health_is_stale(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_servers SET last_health_at = NULL WHERE code = ?",
                ("nl-1",),
            )

        with self.assertRaisesRegex(BusinessRuleError, "недоступен"):
            self._create()

        with self.db.transaction() as conn:
            payments = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_payments"
            ).fetchone()["count"]
        self.assertEqual(payments, 0)
        self.assertEqual(self.client.created, [])

    def test_pending_and_forged_callback_status_never_issue_vpn(self) -> None:
        order = self._create()
        outcome = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"], "status": "CONFIRMED"},
        )

        self.assertEqual(outcome.status, "pending")
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(self.client.get_calls, [order["external_id"]])

    def test_confirmed_payment_is_fulfilled_exactly_once(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)

        first = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        duplicate = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"]},
        )

        self.assertTrue(first.confirmed)
        self.assertTrue(first.processed)
        self.assertFalse(duplicate.processed)
        self.assertEqual(first.subscription["id"], duplicate.subscription["id"])
        self.assertEqual(self._counts(), (1, 1))

    def test_confirmed_vpn_payment_credits_and_chargeback_reverses_referral(
        self,
    ) -> None:
        referrals = ReferralService(self.db)
        applied = referrals.apply_start_referral(
            user_id=int(self.user["id"]),
            start_text=f"/start ref_{self.other['referral_code']}",
        )
        self.assertTrue(applied.assigned)
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)

        paid = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(order["id"]),
        )
        duplicate = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(order["id"]),
        )

        self.assertTrue(paid.processed)
        self.assertFalse(duplicate.processed)
        self.assertEqual(
            referrals.stats(int(self.other["id"])).balance_kopecks,
            5_670,
        )

        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)
        reversed_payment = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(order["id"]),
        )
        repeated_reversal = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(order["id"]),
        )

        self.assertTrue(reversed_payment.processed)
        self.assertFalse(repeated_reversal.processed)
        self.assertEqual(
            referrals.stats(int(self.other["id"])).balance_kopecks,
            0,
        )
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT type, amount_kopecks
                FROM referral_transactions
                WHERE vpn_payment_provider = 'platega'
                  AND vpn_payment_id = ?
                ORDER BY id
                """,
                (int(order["id"]),),
            ).fetchall()
        self.assertEqual(
            [(row["type"], row["amount_kopecks"]) for row in rows],
            [("credit", 5_670), ("adjustment", -5_670)],
        )

    def test_amount_or_currency_mismatch_rolls_back_fulfillment(self) -> None:
        order = self._create()
        self.client.set_status(
            order["external_id"],
            PLATEGA_CONFIRMED,
            amount_rub=188,
        )
        with self.assertRaisesRegex(
            VpnPaymentVerificationError, "Сумма или валюта"
        ):
            self.vpn.check_platega_payment(
                user_id=int(self.user["id"]), payment_id=int(order["id"])
            )

        stored = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(self._counts(), (0, 0))

    def test_callback_auth_and_unknown_transaction_are_safe(self) -> None:
        order = self._create()
        with self.assertRaises(PlategaCallbackAuthenticationError):
            self.vpn.handle_platega_callback(
                headers={"X-MerchantId": "merchant", "X-Secret": "wrong"},
                payload={"id": order["external_id"]},
            )
        with self.assertRaisesRegex(
            VpnPaymentVerificationError, "Некорректный"
        ):
            self.vpn.handle_platega_callback(
                headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
                payload={"id": "not-a-uuid"},
            )
        ignored = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": str(uuid.uuid4())},
        )

        self.assertEqual(ignored.status, "ignored")
        self.assertEqual(self.client.get_calls, [])
        self.assertEqual(self._counts(), (0, 0))

    def test_cancelled_payment_closes_order_and_allows_a_new_one(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CANCELED)
        cancelled = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        replacement = self._create()

        self.assertEqual(cancelled.status, "cancelled")
        self.assertNotEqual(replacement["id"], order["id"])
        self.assertEqual(self._counts(), (0, 0))

    def test_expired_pending_link_is_verified_closed_and_replaced(self) -> None:
        order = self._create()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_payments SET expires_at = ? WHERE id = ?",
                (
                    (utcnow() - timedelta(seconds=1)).isoformat(),
                    int(order["id"]),
                ),
            )

        replacement = self._create()

        self.assertNotEqual(replacement["id"], order["id"])
        self.assertEqual(len(self.client.created), 2)
        self.assertEqual(self.client.get_calls, [order["external_id"]])
        stored = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(replacement["status"], "pending")

    def test_late_confirmed_failed_order_and_replacement_both_count_once(
        self,
    ) -> None:
        old_order = self._create()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_payments SET expires_at = ? WHERE id = ?",
                (
                    (utcnow() - timedelta(seconds=1)).isoformat(),
                    int(old_order["id"]),
                ),
            )
        replacement = self._create()
        self.client.set_status(replacement["external_id"], PLATEGA_CONFIRMED)
        replacement_paid = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(replacement["id"]),
        )

        self.client.set_status(old_order["external_id"], PLATEGA_CONFIRMED)
        recovered = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(old_order["id"]),
        )
        recovered_end = parse_iso(str(recovered.subscription["ends_at"]))
        duplicate = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(old_order["id"]),
        )

        self.assertTrue(recovered.processed)
        self.assertFalse(duplicate.processed)
        self.assertEqual(
            recovered.subscription["id"], replacement_paid.subscription["id"]
        )
        self.assertEqual(
            recovered_end,
            parse_iso(str(replacement_paid.subscription["ends_at"]))
            + timedelta(days=30),
        )
        self.assertEqual(
            parse_iso(str(duplicate.subscription["ends_at"])), recovered_end
        )
        self.assertEqual(self._counts(), (1, 2))
        self.assertEqual(
            self.client.get_calls.count(old_order["external_id"]),
            3,
        )

    def test_maintenance_closes_expired_and_recovers_recent_failed(self) -> None:
        order = self._create()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_payments SET expires_at = ? WHERE id = ?",
                (
                    (utcnow() - timedelta(seconds=1)).isoformat(),
                    int(order["id"]),
                ),
            )

        closed = self.vpn.reconcile_platega_payments(batch_size=4)
        failed = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        failed_at = failed["updated_at"]
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        recovered = self.vpn.reconcile_platega_payments(batch_size=4)
        paid = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )

        self.assertEqual(closed, 1)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["updated_at"], failed_at)
        self.assertEqual(recovered, 1)
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(self._counts(), (1, 1))

    def test_maintenance_catches_missed_chargeback_and_reverses_referral(
        self,
    ) -> None:
        referrals = ReferralService(self.db)
        applied = referrals.apply_start_referral(
            user_id=int(self.user["id"]),
            start_text=f"/start ref_{self.other['referral_code']}",
        )
        self.assertTrue(applied.assigned)
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        paid = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]),
            payment_id=int(order["id"]),
        )
        self.assertEqual(
            referrals.stats(int(self.other["id"])).balance_kopecks,
            5_670,
        )

        # Simulate a lost callback: only the background reconciler sees the
        # provider's authoritative CHARGEBACKED state.
        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)
        self.client.get_calls.clear()
        transitioned = self.vpn.reconcile_platega_payments()
        duplicate = self.vpn.reconcile_platega_payments()

        stored = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        subscription = self.vpn.get_current_subscription(int(self.user["id"]))
        self.assertEqual(transitioned, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(stored["status"], "refunded")
        self.assertEqual(subscription["id"], paid.subscription["id"])
        self.assertEqual(subscription["status"], "disabled")
        self.assertEqual(
            referrals.stats(int(self.other["id"])).balance_kopecks,
            0,
        )
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT type, amount_kopecks
                FROM referral_transactions
                WHERE vpn_payment_provider = 'platega'
                  AND vpn_payment_id = ?
                ORDER BY id
                """,
                (int(order["id"]),),
            ).fetchall()
        self.assertEqual(
            [(row["type"], row["amount_kopecks"]) for row in rows],
            [("credit", 5_670), ("adjustment", -5_670)],
        )

    def test_paid_maintenance_slice_is_bounded_and_circular(self) -> None:
        orders = []
        for _ in range(3):
            order = self._create()
            self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
            self.vpn.check_platega_payment(
                user_id=int(self.user["id"]),
                payment_id=int(order["id"]),
            )
            orders.append(order)

        self.client.get_calls.clear()
        self.vpn.reconcile_platega_payments()
        first_slice = list(self.client.get_calls)
        self.client.get_calls.clear()
        self.vpn.reconcile_platega_payments()
        second_slice = list(self.client.get_calls)

        self.assertEqual(len(first_slice), 2)
        self.assertEqual(len(second_slice), 2)
        self.assertEqual(
            set(first_slice),
            {orders[0]["external_id"], orders[1]["external_id"]},
        )
        self.assertIn(orders[2]["external_id"], second_slice)

    def test_failed_pending_recheck_does_not_extend_recovery_window(self) -> None:
        order = self._create()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE vpn_payments
                SET status = 'failed', updated_at = ?
                WHERE id = ?
                """,
                (utcnow().isoformat(), int(order["id"])),
            )
        before = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )["updated_at"]

        self.assertEqual(
            self.vpn.reconcile_platega_payments(batch_size=4),
            0,
        )
        after = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )["updated_at"]

        self.assertEqual(after, before)

    def test_maintenance_is_bounded_rotates_and_isolates_provider_errors(
        self,
    ) -> None:
        first = self._create(plan_code="vpn-1m")
        second = self._create(plan_code="vpn-3m")
        third = self.vpn.create_platega_payment(
            user_id=int(self.other["id"]),
            plan_code="vpn-1m",
            user_name="other_user",
        )[0]
        self.client.set_status(second["external_id"], PLATEGA_CONFIRMED)
        original_get = self.client.get_transaction

        def get_with_one_failure(transaction_id: str) -> PlategaTransaction:
            if transaction_id == first["external_id"]:
                self.client.get_calls.append(transaction_id)
                raise PlategaRequestError("provider detail must stay private")
            return original_get(transaction_id)

        self.client.get_transaction = get_with_one_failure
        with self.assertLogs(level="WARNING") as logs:
            transitioned = self.vpn.reconcile_platega_payments(batch_size=2)

        self.assertEqual(transitioned, 1)
        self.assertEqual(len(self.client.get_calls), 2)
        self.assertNotIn("provider detail must stay private", "\n".join(logs.output))
        paid_second = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(second["id"])
        )
        self.assertEqual(paid_second["status"], "paid")

        self.client.get_calls.clear()
        self.vpn.reconcile_platega_payments(batch_size=2)
        self.assertIn(third["external_id"], self.client.get_calls)
        self.assertLessEqual(len(self.client.get_calls), 2)

    def test_disabled_provider_maintenance_is_a_noop(self) -> None:
        order = self._create()
        self.client.get_calls.clear()
        disabled = VpnService(
            self.db,
            server_code="nl-1",
            payment_provider="disabled",
            platega_client=self.client,  # type: ignore[arg-type]
        )

        self.assertEqual(disabled.reconcile_platega_payments(), 0)
        self.assertEqual(self.client.get_calls, [])
        stored = disabled.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.assertEqual(stored["status"], "pending")

    def test_chargeback_before_worker_supersedes_job_and_never_delivers_key(
        self,
    ) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        paid = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        paid_end = parse_iso(str(paid.subscription["ends_at"]))
        running = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.assertEqual(running["operation"], "create")

        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)
        first = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"]},
        )
        duplicate = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"]},
        )

        self.assertEqual(first.status, "refunded")
        self.assertTrue(first.processed)
        self.assertFalse(duplicate.processed)
        with self.assertRaisesRegex(BusinessRuleError, "lease"):
            self.vpn.complete_worker_job(
                worker_id="worker-nl1",
                job_id=int(running["job_id"]),
                lease_token=str(running["lease_token"]),
                subscription_url="https://sub.example.test/sub/reversed",
            )

        disable = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.assertEqual(disable["operation"], "disable")
        with self.db.transaction() as conn:
            subscription = conn.execute(
                "SELECT * FROM vpn_subscriptions WHERE id = ?",
                (int(paid.subscription["id"]),),
            ).fetchone()
            jobs = conn.execute(
                """
                SELECT operation, status
                FROM vpn_provisioning_jobs
                WHERE subscription_id = ?
                ORDER BY id
                """,
                (int(paid.subscription["id"]),),
            ).fetchall()
        self.assertEqual(subscription["status"], "disabled")
        self.assertEqual(
            parse_iso(str(subscription["ends_at"])),
            paid_end - timedelta(days=30),
        )
        self.assertEqual(
            [(row["operation"], row["status"]) for row in jobs],
            [("create", "completed"), ("disable", "running")],
        )

    def test_pending_chargeback_releases_order_for_replacement(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)

        reversed_order = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"]},
        )
        replacement = self._create()

        self.assertEqual(reversed_order.status, "refunded")
        self.assertNotEqual(replacement["id"], order["id"])
        self.assertEqual(self._counts(), (0, 0))

    def test_stacked_chargeback_subtracts_once_and_restores_remaining_plan(
        self,
    ) -> None:
        first_order = self._create()
        self.client.set_status(first_order["external_id"], PLATEGA_CONFIRMED)
        first = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(first_order["id"])
        )
        second_order = self._create(plan_code="vpn-3m")
        self.client.set_status(second_order["external_id"], PLATEGA_CONFIRMED)
        second = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(second_order["id"])
        )
        self.assertEqual(first.subscription["id"], second.subscription["id"])
        stacked_end = parse_iso(str(second.subscription["ends_at"]))

        self.client.set_status(second_order["external_id"], PLATEGA_CHARGEBACKED)
        chargeback = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(second_order["id"])
        )
        duplicate = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(second_order["id"])
        )

        self.assertEqual(chargeback.status, "refunded")
        self.assertTrue(chargeback.processed)
        self.assertFalse(duplicate.processed)
        with self.db.transaction() as conn:
            subscription = conn.execute(
                """
                SELECT subscription.*, plan.code AS plan_code
                FROM vpn_subscriptions subscription
                LEFT JOIN vpn_plans plan ON plan.id = subscription.plan_id
                WHERE subscription.id = ?
                """,
                (int(first.subscription["id"]),),
            ).fetchone()
            jobs = conn.execute(
                """
                SELECT operation, status, idempotency_key
                FROM vpn_provisioning_jobs
                WHERE subscription_id = ?
                ORDER BY id
                """,
                (int(first.subscription["id"]),),
            ).fetchall()
        self.assertEqual(subscription["status"], "provisioning")
        self.assertEqual(
            parse_iso(str(subscription["ends_at"])),
            stacked_end - timedelta(days=90),
        )
        self.assertEqual(subscription["plan_code"], "vpn-1m")
        self.assertIsNone(subscription["last_error"])
        self.assertEqual(
            [
                (row["operation"], row["status"], row["idempotency_key"])
                for row in jobs
            ],
            [
                (
                    "create",
                    "pending",
                    f"vpn:payment:{first_order['id']}:create",
                ),
                (
                    "update",
                    "completed",
                    f"vpn:payment:{second_order['id']}:update",
                ),
                (
                    "update",
                    "pending",
                    f"vpn:chargeback:{second_order['id']}:update",
                ),
            ],
        )

    def test_trial_chargeback_restores_trial_and_suppresses_ready_notice(
        self,
    ) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        trial_end = parse_iso(str(trial.subscription["ends_at"]))
        create_job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(create_job["job_id"]),
            lease_token=str(create_job["lease_token"]),
            subscription_url="https://sub.example.test/sub/trial-key",
        )

        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        paid = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.assertEqual(
            parse_iso(str(paid.subscription["ends_at"])),
            trial_end + timedelta(days=30),
        )
        paid_update = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(paid_update["job_id"]),
            lease_token=str(paid_update["lease_token"]),
            subscription_url="https://sub.example.test/sub/trial-key",
        )

        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)
        chargeback = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        correction = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )

        self.assertEqual(chargeback.status, "refunded")
        self.assertEqual(correction["operation"], "update")
        self.assertEqual(
            correction["marzban_user"]["expire"],
            int(trial_end.timestamp()),
        )
        completion = self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(correction["job_id"]),
            lease_token=str(correction["lease_token"]),
            subscription_url="https://sub.example.test/sub/trial-key",
        )
        self.assertEqual(completion.operation, "update")
        self.assertEqual(completion.subscription["subscription_url"], "")

        stored = self.vpn.get_current_subscription(int(self.user["id"]))
        self.assertEqual(stored["status"], "active")
        self.assertEqual(parse_iso(str(stored["ends_at"])), trial_end)
        self.assertEqual(stored["billing_kind"], "trial")
        self.assertIsNone(stored["plan_id"])
        self.assertEqual(
            stored["subscription_url"],
            "https://sub.example.test/sub/trial-key",
        )

    def test_another_user_cannot_check_payment(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)

        with self.assertRaisesRegex(BusinessRuleError, "Заказ не найден"):
            self.vpn.check_platega_payment(
                user_id=int(self.other["id"]), payment_id=int(order["id"])
            )
        self.assertEqual(self._counts(), (0, 0))

    def test_authenticated_chargeback_marks_paid_order_refunded(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)

        chargeback = self.vpn.handle_platega_callback(
            headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
            payload={"id": order["external_id"]},
        )

        self.assertEqual(chargeback.status, "refunded")
        self.assertTrue(chargeback.processed)
        stored = self.vpn.get_payment_for_user(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.assertEqual(stored["status"], "refunded")
        self.assertEqual(self._counts(), (1, 2))

    def test_manual_check_refetches_locally_paid_order_for_chargeback(self) -> None:
        order = self._create()
        self.client.set_status(order["external_id"], PLATEGA_CONFIRMED)
        self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )
        self.client.set_status(order["external_id"], PLATEGA_CHARGEBACKED)

        outcome = self.vpn.check_platega_payment(
            user_id=int(self.user["id"]), payment_id=int(order["id"])
        )

        self.assertEqual(outcome.status, "refunded")
        self.assertEqual(
            self.client.get_calls,
            [order["external_id"], order["external_id"]],
        )


if __name__ == "__main__":
    unittest.main()
