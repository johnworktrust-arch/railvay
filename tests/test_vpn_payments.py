from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from ceai.config import load_settings
from ceai.database import Database
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.services.exceptions import BusinessRuleError
from ceai.services.users import UserService
from ceai.services.vpn import VpnService
from ceai.time_utils import parse_iso
from ceai.vpn_bot.handlers import _admin_demo_authorized, _payment_callback_id


class VpnPaymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        users = UserService(self.db)
        self.owner = users.ensure_telegram_user(
            telegram_id=9101,
            username="vpn_owner",
            first_name="Owner",
            last_name="VPN",
            language_code="ru",
        )
        self.other = users.ensure_telegram_user(
            telegram_id=9102,
            username="vpn_other",
            first_name="Other",
            last_name="VPN",
            language_code="ru",
        )
        with self.db.transaction() as conn:
            VpnServerRepository().upsert(
                conn,
                code="nl-1",
                name="Amsterdam 1",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl1",
                subscription_base_url="https://sub.example.test:8443",
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
                price_rub=479,
                price_stars=399,
                max_devices=3,
            )
        self.vpn = VpnService(
            self.db,
            server_code="nl-1",
            trial_days=3,
            allow_admin_demo_payment=True,
        )

    def tearDown(self) -> None:
        self.db.close()

    def _counts(self) -> tuple[int, int]:
        with self.db.transaction() as conn:
            subscriptions = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_subscriptions"
            ).fetchone()["count"]
            jobs = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_provisioning_jobs"
            ).fetchone()["count"]
        return int(subscriptions), int(jobs)

    def _new_order(
        self,
        *,
        user_id: int | None = None,
        method: str = "sbp",
        plan_code: str = "vpn-1m",
    ):
        return self.vpn.create_admin_demo_payment(
            user_id=user_id or int(self.owner["id"]),
            plan_code=plan_code,
            payment_method=method,
            admin_authorized=True,
        )[0]

    def _complete_next_job(self, *, suffix: str) -> str:
        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=job["job_id"],
            lease_token=job["lease_token"],
            subscription_url=(
                "" if job["operation"] == "disable"
                else f"https://sub.example.test:8443/sub/{suffix}"
            ),
        )
        return str(job["operation"])

    def test_pending_demo_order_never_enqueues_or_issues_vpn(self) -> None:
        order = self._new_order()

        self.assertEqual(order["status"], "pending")
        self.assertIsNone(order["vpn_subscription_id"])
        self.assertEqual(self._counts(), (0, 0))
        checked = self.vpn.get_payment_for_user(
            user_id=int(self.owner["id"]),
            payment_id=int(order["id"]),
        )
        self.assertEqual(checked["status"], "pending")
        self.assertEqual(self._counts(), (0, 0))

    def test_admin_demo_requires_both_flag_and_admin_authorization(self) -> None:
        with self.assertRaisesRegex(BusinessRuleError, "только владельцу"):
            self.vpn.create_admin_demo_payment(
                user_id=int(self.other["id"]),
                plan_code="vpn-1m",
                payment_method="sbp",
                admin_authorized=False,
            )

        disabled = VpnService(
            self.db,
            server_code="nl-1",
            trial_days=3,
            allow_admin_demo_payment=False,
        )
        with self.assertRaisesRegex(BusinessRuleError, "только владельцу"):
            disabled.create_admin_demo_payment(
                user_id=int(self.owner["id"]),
                plan_code="vpn-1m",
                payment_method="sbp",
                admin_authorized=True,
            )
        self.assertEqual(self._counts(), (0, 0))

    def test_admin_demo_flag_defaults_off_and_reads_explicit_env(self) -> None:
        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {"TELEGRAM_BOT_TOKEN": "test"},
                clear=True,
            ),
        ):
            self.assertFalse(load_settings().vpn_allow_admin_demo_payment)

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "VPN_ALLOW_ADMIN_DEMO_PAYMENT": "1",
                    "VPN_ADMIN_DEMO_TELEGRAM_IDS": "9101, 9103",
                },
                clear=True,
            ),
        ):
            settings = load_settings()
            self.assertTrue(settings.vpn_allow_admin_demo_payment)
            self.assertEqual(settings.vpn_admin_demo_telegram_ids, (9101, 9103))

    def test_callback_ids_and_numeric_owner_allowlist_are_strict(self) -> None:
        self.assertEqual(_payment_callback_id("vpn:check:42", "vpn:check"), 42)
        self.assertIsNone(
            _payment_callback_id("vpn:check:anything:42", "vpn:check")
        )
        self.assertIsNone(_payment_callback_id("vpn:check:0", "vpn:check"))
        self.assertIsNone(
            _payment_callback_id(
                "vpn:check:9223372036854775808",
                "vpn:check",
            )
        )

        settings = SimpleNamespace(
            vpn_allow_admin_demo_payment=True,
            vpn_admin_demo_telegram_ids=(9101,),
        )
        services = SimpleNamespace(settings=settings)
        owner_event = SimpleNamespace(from_user=SimpleNamespace(id=9101))
        other_event = SimpleNamespace(from_user=SimpleNamespace(id=9102))
        self.assertTrue(_admin_demo_authorized(owner_event, services))
        self.assertFalse(_admin_demo_authorized(other_event, services))
        settings.vpn_allow_admin_demo_payment = False
        self.assertFalse(_admin_demo_authorized(owner_event, services))

    def test_another_user_cannot_confirm_or_read_order(self) -> None:
        order = self._new_order()
        self.assertIsNone(
            self.vpn.get_payment_for_user(
                user_id=int(self.other["id"]),
                payment_id=int(order["id"]),
            )
        )
        with self.assertRaisesRegex(BusinessRuleError, "Заказ не найден"):
            self.vpn.confirm_admin_demo_payment(
                user_id=int(self.other["id"]),
                payment_id=int(order["id"]),
                admin_authorized=True,
            )
        self.assertEqual(self._counts(), (0, 0))

    def test_confirmed_payment_creates_exactly_one_subscription_and_job(self) -> None:
        order = self._new_order()
        first = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(order["id"]),
            admin_authorized=True,
        )
        second = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(order["id"]),
            admin_authorized=True,
        )

        self.assertTrue(first.processed)
        self.assertFalse(second.processed)
        self.assertEqual(first.payment["status"], "paid")
        self.assertEqual(first.subscription["kind"], "paid")
        self.assertEqual(first.subscription["billing_kind"], "paid")
        self.assertEqual(first.subscription["id"], second.subscription["id"])
        self.assertEqual(self._counts(), (1, 1))
        with self.db.transaction() as conn:
            job = conn.execute(
                "SELECT operation, idempotency_key FROM vpn_provisioning_jobs"
            ).fetchone()
        self.assertEqual(job["operation"], "create")
        self.assertEqual(
            job["idempotency_key"],
            f"vpn:payment:{order['id']}:create",
        )

    def test_pending_order_keeps_its_price_and_duration_snapshot(self) -> None:
        order = self._new_order()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE vpn_plans
                SET price_rub = 190, duration_days = 60, is_active = FALSE
                WHERE code = 'vpn-1m'
                """
            )

        outcome = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(order["id"]),
            admin_authorized=True,
        )

        self.assertEqual(outcome.payment["status"], "paid")
        self.assertEqual(outcome.payment["amount_rub"], 189)
        self.assertEqual(outcome.payment["duration_days"], 30)
        period = (
            parse_iso(outcome.subscription["ends_at"])
            - parse_iso(outcome.subscription["starts_at"])
        )
        self.assertEqual(period, timedelta(days=30))
        self.assertEqual(self._counts(), (1, 1))

    def test_paid_renewal_extends_same_paid_subscription_idempotently(self) -> None:
        first_order = self._new_order()
        first = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(first_order["id"]),
            admin_authorized=True,
        )
        self.assertEqual(self._complete_next_job(suffix="paid-one"), "create")
        active = self.vpn.get_current_subscription(int(self.owner["id"]))
        assert active is not None
        original_end = parse_iso(active["ends_at"])

        second_order = self._new_order(method="card")
        renewal = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(second_order["id"]),
            admin_authorized=True,
        )

        self.assertEqual(renewal.subscription["id"], first.subscription["id"])
        renewed_end = parse_iso(renewal.subscription["ends_at"])
        self.assertEqual(renewed_end - original_end, timedelta(days=30))
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT operation FROM vpn_provisioning_jobs ORDER BY id"
            ).fetchall()
        self.assertEqual([row["operation"] for row in rows], ["create", "update"])

    def test_old_payment_stays_linked_after_switching_to_another_plan(self) -> None:
        first_order = self._new_order()
        first = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(first_order["id"]),
            admin_authorized=True,
        )
        self.assertEqual(self._complete_next_job(suffix="one-month"), "create")

        second_order = self._new_order(
            method="card",
            plan_code="vpn-3m",
        )
        second = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(second_order["id"]),
            admin_authorized=True,
        )
        repeated_first = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(first_order["id"]),
            admin_authorized=True,
        )
        old_link = self.vpn.get_payment_subscription_for_user(
            user_id=int(self.owner["id"]),
            payment_id=int(first_order["id"]),
        )

        self.assertEqual(second.subscription["id"], first.subscription["id"])
        self.assertEqual(second.subscription["plan_code"], "vpn-3m")
        self.assertFalse(repeated_first.processed)
        self.assertEqual(repeated_first.subscription["id"], first.subscription["id"])
        self.assertIsNotNone(old_link)
        assert old_link is not None
        self.assertEqual(old_link["id"], first.subscription["id"])

    def test_paid_order_extends_active_trial_on_the_same_credential(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.owner["id"]),
            channel="@ceafamily",
        )
        self.assertEqual(self._complete_next_job(suffix="trial"), "create")
        order = self._new_order()
        current_before = self.vpn.get_current_subscription(int(self.owner["id"]))
        self.assertEqual(current_before["id"], trial.subscription["id"])
        self.assertEqual(current_before["kind"], "trial")

        paid = self.vpn.confirm_admin_demo_payment(
            user_id=int(self.owner["id"]),
            payment_id=int(order["id"]),
            admin_authorized=True,
        )

        self.assertEqual(paid.subscription["id"], trial.subscription["id"])
        self.assertEqual(paid.subscription["kind"], "trial")
        self.assertEqual(paid.subscription["billing_kind"], "paid")
        self.assertEqual(paid.subscription["plan_code"], "vpn-1m")
        with self.db.transaction() as conn:
            jobs = conn.execute(
                "SELECT operation FROM vpn_provisioning_jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["operation"] for row in jobs],
            ["create", "update"],
        )
        self.assertEqual(self._complete_next_job(suffix="paid-after-trial"), "update")
        current = self.vpn.get_current_subscription(int(self.owner["id"]))
        self.assertEqual(current["id"], paid.subscription["id"])
        self.assertEqual(current["status"], "active")

    def test_trial_remains_the_explicit_free_exception(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.other["id"]),
            channel="@ceafamily",
        )
        self.assertTrue(trial.created)
        self.assertEqual(trial.subscription["kind"], "trial")
        with self.db.transaction() as conn:
            payments = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_payments"
            ).fetchone()["count"]
        self.assertEqual(payments, 0)


if __name__ == "__main__":
    unittest.main()
