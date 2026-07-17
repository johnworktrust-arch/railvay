from __future__ import annotations

import unittest
from datetime import timedelta

from ceai.database import Database
from ceai.repositories.vpn_payments import (
    ADMIN_DEMO_PROVIDER,
    PLATEGA_PROVIDER,
    VpnPaymentRepository,
)
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceai.services.users import UserService
from ceai.time_utils import iso_now, parse_iso


class VpnPlategaPaymentRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        self.payments = VpnPaymentRepository()
        self.subscriptions = VpnSubscriptionRepository()
        user = UserService(self.db).ensure_telegram_user(
            telegram_id=78001,
            username="platega_repo",
            first_name="Platega",
            last_name="Repository",
            language_code="ru",
        )
        self.user_id = int(user["id"])

        with self.db.transaction() as conn:
            server = VpnServerRepository().upsert(
                conn,
                code="platega-nl",
                name="Platega Netherlands",
                provider="marzban",
                region="NL",
                api_base_url="https://worker.example.test",
                worker_id="worker-platega",
                subscription_base_url="https://sub.example.test",
            )
            plan = VpnPlanRepository().upsert(
                conn,
                code="platega-1m",
                name="1 месяц",
                duration_days=30,
                price_rub=189,
                price_stars=149,
                max_devices=3,
            )
        self.server_id = int(server["id"])
        self.plan_id = int(plan["id"])
        self._sequence = 0

    def tearDown(self) -> None:
        self.db.close()

    def _reserve(
        self,
        *,
        method: str = "sbp",
        amount_rub: int = 189,
        duration_days: int = 30,
    ) -> tuple[dict, bool]:
        self._sequence += 1
        with self.db.transaction() as conn:
            return self.payments.create_or_get_pending_platega(
                conn,
                user_id=self.user_id,
                plan_id=self.plan_id,
                amount_rub=amount_rub,
                duration_days=duration_days,
                payment_method=method,
                request_external_id=f"platega_request_{self._sequence}",
            )

    def _attach(
        self,
        payment: dict,
        *,
        external_id: str,
        payment_url: str | None = None,
        expires_at: str | None = "2030-01-02T03:04:05Z",
    ) -> dict:
        with self.db.transaction() as conn:
            return self.payments.attach_platega_transaction(
                conn,
                payment_id=int(payment["id"]),
                user_id=self.user_id,
                expected_external_id=str(payment["external_id"]),
                external_id=external_id,
                payment_url=payment_url
                or f"https://pay.platega.io/transaction/{external_id}",
                expires_at=expires_at,
            )

    def test_migration_adds_provider_fields_and_pending_index(self) -> None:
        with self.db.transaction() as conn:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(vpn_payments)").fetchall()
            }
            indexes = {
                str(row["name"])
                for row in conn.execute("PRAGMA index_list(vpn_payments)").fetchall()
            }

        self.assertIn("payment_url", columns)
        self.assertIn("expires_at", columns)
        self.assertIn("idx_vpn_payments_one_pending_platega", indexes)
        self.assertIn(
            "idx_vpn_payments_platega_pending_reconciliation", indexes
        )
        self.assertIn(
            "idx_vpn_payments_platega_failed_reconciliation", indexes
        )
        self.assertIn(
            "idx_vpn_payments_platega_paid_reconciliation", indexes
        )

    def test_one_pending_order_keeps_first_price_and_duration_snapshot(self) -> None:
        first, first_created = self._reserve()
        second, second_created = self._reserve(
            amount_rub=999,
            duration_days=365,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["external_id"], first["external_id"])
        self.assertEqual(second["amount_rub"], 189)
        self.assertEqual(second["duration_days"], 30)
        self.assertEqual(second["provider"], PLATEGA_PROVIDER)
        self.assertIsNone(second["payment_url"])

    def test_attach_is_compare_and_set_idempotent_and_url_is_immutable(self) -> None:
        reserved, _ = self._reserve()
        attached = self._attach(reserved, external_id="tx-immutable")

        self.assertEqual(attached["external_id"], "tx-immutable")
        self.assertEqual(
            attached["payment_url"],
            "https://pay.platega.io/transaction/tx-immutable",
        )
        self.assertEqual(attached["expires_at"], "2030-01-02T03:04:05+00:00")
        with self.db.transaction() as conn:
            repeated = self.payments.attach_platega_transaction(
                conn,
                payment_id=int(attached["id"]),
                user_id=self.user_id,
                expected_external_id=str(reserved["external_id"]),
                external_id="tx-immutable",
                payment_url=str(attached["payment_url"]),
                expires_at="2030-01-02T03:04:05+00:00",
            )
            looked_up = self.payments.get_by_provider_external_id(
                conn,
                provider=PLATEGA_PROVIDER,
                external_id="tx-immutable",
            )
        self.assertEqual(repeated["id"], attached["id"])
        self.assertEqual(looked_up["id"], attached["id"])

        with self.db.transaction() as conn:
            with self.assertRaisesRegex(ValueError, "already attached"):
                self.payments.attach_platega_transaction(
                    conn,
                    payment_id=int(attached["id"]),
                    user_id=self.user_id,
                    expected_external_id="tx-immutable",
                    external_id="tx-replacement",
                    payment_url="https://pay.platega.io/replacement",
                    expires_at=None,
                )

    def test_attach_cannot_use_another_pending_order_placeholder(self) -> None:
        first, _ = self._reserve(method="sbp")
        second, _ = self._reserve(method="card")

        with self.db.transaction() as conn:
            with self.assertRaisesRegex(ValueError, "placeholder does not match"):
                self.payments.attach_platega_transaction(
                    conn,
                    payment_id=int(first["id"]),
                    user_id=self.user_id,
                    expected_external_id=str(second["external_id"]),
                    external_id="tx-wrong-order",
                    payment_url="https://pay.platega.io/wrong-order",
                    expires_at=None,
                )
            unchanged = self.payments.get_by_id(conn, int(first["id"]))

        self.assertEqual(unchanged["external_id"], first["external_id"])
        self.assertIsNone(unchanged["payment_url"])

    def test_attach_rejects_untrusted_payment_url_authorities(self) -> None:
        reserved, _ = self._reserve()
        invalid_urls = (
            "https://pay.platega.io.evil.example/pay",
            "https://user@pay.platega.io/pay",
            "https://pay.platega.io:444/pay",
        )

        for url in invalid_urls:
            with self.subTest(url=url), self.db.transaction() as conn:
                with self.assertRaisesRegex(ValueError, "trusted Platega"):
                    self.payments.attach_platega_transaction(
                        conn,
                        payment_id=int(reserved["id"]),
                        user_id=self.user_id,
                        expected_external_id=str(reserved["external_id"]),
                        external_id="tx-untrusted-url",
                        payment_url=url,
                        expires_at=None,
                    )

        with self.db.transaction() as conn:
            unchanged = self.payments.get_by_id(conn, int(reserved["id"]))
        self.assertEqual(unchanged["external_id"], reserved["external_id"])
        self.assertIsNone(unchanged["payment_url"])

    def test_mark_paid_requires_provider_and_external_id_and_is_idempotent(self) -> None:
        reserved, _ = self._reserve()
        attached = self._attach(reserved, external_id="tx-confirmed")

        with self.db.transaction() as conn:
            with self.assertRaisesRegex(ValueError, "another provider"):
                self.payments.mark_paid(
                    conn,
                    payment_id=int(attached["id"]),
                    expected_provider=ADMIN_DEMO_PROVIDER,
                    expected_external_id="tx-confirmed",
                )
            with self.assertRaisesRegex(ValueError, "external ID does not match"):
                self.payments.mark_paid(
                    conn,
                    payment_id=int(attached["id"]),
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id="tx-other",
                )
            paid, changed = self.payments.mark_paid(
                conn,
                payment_id=int(attached["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-confirmed",
                user_id=self.user_id,
            )
            repeated, repeated_changed = self.payments.mark_paid(
                conn,
                payment_id=int(attached["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-confirmed",
                user_id=self.user_id,
            )

        self.assertTrue(changed)
        self.assertFalse(repeated_changed)
        self.assertEqual(paid["status"], "paid")
        self.assertIsNotNone(paid["paid_at"])
        self.assertEqual(repeated["paid_at"], paid["paid_at"])

    def test_terminal_provider_transitions_are_narrow_and_idempotent(self) -> None:
        cancellable, _ = self._reserve(method="sbp")
        cancellable = self._attach(cancellable, external_id="tx-cancelled")
        with self.db.transaction() as conn:
            cancelled, changed = self.payments.mark_provider_status(
                conn,
                payment_id=int(cancellable["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-cancelled",
                status="cancelled",
            )
            repeated, repeated_changed = self.payments.mark_provider_status(
                conn,
                payment_id=int(cancellable["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-cancelled",
                status="cancelled",
            )
            with self.assertRaisesRegex(ValueError, "cannot be paid"):
                self.payments.mark_paid(
                    conn,
                    payment_id=int(cancellable["id"]),
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id="tx-cancelled",
                )
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                self.payments.mark_provider_status(
                    conn,
                    payment_id=int(cancellable["id"]),
                    expected_provider=PLATEGA_PROVIDER,
                    status="chargebacked",
                )

        self.assertTrue(changed)
        self.assertFalse(repeated_changed)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(repeated["status"], "cancelled")

        refundable, _ = self._reserve(method="card")
        refundable = self._attach(refundable, external_id="tx-refunded")
        with self.db.transaction() as conn:
            self.payments.mark_paid(
                conn,
                payment_id=int(refundable["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-refunded",
            )
            refunded, refunded_changed = self.payments.mark_provider_status(
                conn,
                payment_id=int(refundable["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-refunded",
                status="refunded",
            )
            repeated_refund, repeated_refund_changed = (
                self.payments.mark_provider_status(
                    conn,
                    payment_id=int(refundable["id"]),
                    expected_provider=PLATEGA_PROVIDER,
                    expected_external_id="tx-refunded",
                    status="refunded",
                )
            )
        self.assertTrue(refunded_changed)
        self.assertFalse(repeated_refund_changed)
        self.assertEqual(refunded["status"], "refunded")
        self.assertEqual(repeated_refund["status"], "refunded")
        self.assertIsNotNone(refunded["paid_at"])

    def test_paid_platega_order_can_link_only_matching_paid_subscription(self) -> None:
        reserved, _ = self._reserve()
        attached = self._attach(reserved, external_id="tx-link")
        with self.db.transaction() as conn:
            paid, _ = self.payments.mark_paid(
                conn,
                payment_id=int(attached["id"]),
                expected_provider=PLATEGA_PROVIDER,
                expected_external_id="tx-link",
                user_id=self.user_id,
            )
            starts_at = iso_now()
            subscription = self.subscriptions.create_provisioning(
                conn,
                user_id=self.user_id,
                server_id=self.server_id,
                plan_id=self.plan_id,
                kind="paid",
                provider_username="paid_platega_repo",
                starts_at=starts_at,
                ends_at=(parse_iso(starts_at) + timedelta(days=30)).isoformat(),
            )
            linked = self.payments.link_subscription(
                conn,
                payment_id=int(paid["id"]),
                user_id=self.user_id,
                subscription_id=int(subscription["id"]),
                expected_provider=PLATEGA_PROVIDER,
            )
            repeated = self.payments.link_subscription(
                conn,
                payment_id=int(paid["id"]),
                user_id=self.user_id,
                subscription_id=int(subscription["id"]),
                expected_provider=PLATEGA_PROVIDER,
            )

        self.assertEqual(linked["vpn_subscription_id"], subscription["id"])
        self.assertEqual(repeated["vpn_subscription_id"], subscription["id"])


if __name__ == "__main__":
    unittest.main()
