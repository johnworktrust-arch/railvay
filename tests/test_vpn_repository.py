from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path

from ceai.database import Database
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceai.repositories.vpn_trial_claims import VpnTrialClaimRepository
from ceai.services.users import UserService
from ceai.time_utils import utcnow


class VpnRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        self.user = UserService(self.db).ensure_telegram_user(
            telegram_id=7001,
            username="vpn_tester",
            first_name="VPN",
            last_name="Tester",
            language_code="ru",
        )
        self.servers = VpnServerRepository()
        self.plans = VpnPlanRepository()
        self.subscriptions = VpnSubscriptionRepository()
        self.trials = VpnTrialClaimRepository()
        self.jobs = VpnProvisioningJobRepository()

        with self.db.transaction() as conn:
            self.server = self.servers.upsert(
                conn,
                code="de-1",
                name="Germany 1",
                provider="marzban",
                region="DE",
                api_base_url="https://vpn1.example.test/",
            )
            self.plan = self.plans.upsert(
                conn,
                code="vpn-1m",
                name="1 месяц",
                duration_days=30,
                price_rub=189,
                price_stars=149,
                max_devices=3,
            )

    def tearDown(self) -> None:
        self.db.close()

    def _create_subscription(
        self,
        *,
        provider_username: str = "ceavpn_7001",
        user_id: int | None = None,
        kind: str = "paid",
    ):
        starts_at = utcnow()
        with self.db.transaction() as conn:
            return self.subscriptions.create_provisioning(
                conn,
                user_id=user_id or self.user["id"],
                server_id=self.server["id"],
                plan_id=self.plan["id"] if kind == "paid" else None,
                kind=kind,
                provider_username=provider_username,
                starts_at=starts_at.isoformat(),
                ends_at=(starts_at + timedelta(days=30)).isoformat(),
            )

    def test_vpn_migrations_create_expected_tables_for_both_drivers(self) -> None:
        table_names = {
            "vpn_servers",
            "vpn_plans",
            "vpn_subscriptions",
            "vpn_trial_claims",
            "vpn_provisioning_jobs",
            "vpn_payments",
        }
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'vpn_%'
                """
            ).fetchall()
        self.assertTrue(table_names.issubset({row["name"] for row in rows}))

        vpn_core_tables = table_names - {"vpn_payments"}
        for migration_path in (
            Path("migrations/008_vpn.sql"),
            Path("migrations/postgres/008_vpn.sql"),
        ):
            source = migration_path.read_text(encoding="utf-8")
            for table_name in vpn_core_tables:
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table_name}", source)

        for migration_path in (
            Path("migrations/010_vpn_payments.sql"),
            Path("migrations/postgres/010_vpn_payments.sql"),
        ):
            source = migration_path.read_text(encoding="utf-8")
            self.assertIn("CREATE TABLE IF NOT EXISTS vpn_payments", source)

    def test_server_and_plan_upserts_are_stable(self) -> None:
        self.assertEqual(self.server["api_base_url"], "https://vpn1.example.test")
        self.assertEqual(self.plan["price_stars"], 149)

        with self.db.transaction() as conn:
            updated_server = self.servers.upsert(
                conn,
                code="de-1",
                name="Germany Primary",
                provider="marzban",
                region="DE",
                api_base_url="https://vpn1.example.test",
            )
            updated_plan = self.plans.upsert(
                conn,
                code="vpn-1m",
                name="30 дней",
                duration_days=30,
                price_rub=199,
                price_stars=159,
                max_devices=3,
            )
            active_servers = self.servers.list_active(conn)
            active_plans = self.plans.list_active(conn)

        self.assertEqual(updated_server["id"], self.server["id"])
        self.assertEqual(updated_server["name"], "Germany Primary")
        self.assertEqual(updated_plan["id"], self.plan["id"])
        self.assertEqual(updated_plan["price_rub"], 199)
        self.assertEqual([row["code"] for row in active_servers], ["de-1"])
        self.assertEqual([row["code"] for row in active_plans], ["vpn-1m"])

    def test_only_one_live_subscription_per_user(self) -> None:
        subscription = self._create_subscription()
        with self.db.transaction() as conn:
            activated = self.subscriptions.mark_active(
                conn,
                subscription_id=subscription["id"],
                subscription_url="https://vpn1.example.test/sub/token",
            )
            active = self.subscriptions.get_active_for_user(conn, self.user["id"])

        self.assertEqual(activated["status"], "active")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active["subscription_url"], activated["subscription_url"])
        self.assertEqual(active["server_code"], "de-1")
        self.assertEqual(active["plan_code"], "vpn-1m")

        with self.assertRaises(sqlite3.IntegrityError):
            self._create_subscription(provider_username="ceavpn_7001_second")

        with self.db.transaction() as conn:
            self.subscriptions.mark_status(
                conn,
                subscription_id=subscription["id"],
                status="expired",
            )
        replacement = self._create_subscription(
            provider_username="ceavpn_7001_replacement"
        )
        self.assertEqual(replacement["status"], "provisioning")

    def test_trial_claim_is_idempotent_per_user(self) -> None:
        subscription = self._create_subscription(kind="trial")
        with self.db.transaction() as conn:
            first, first_created = self.trials.create(
                conn,
                user_id=self.user["id"],
                subscription_id=subscription["id"],
                channel="@ceafamily",
            )
            second, second_created = self.trials.create(
                conn,
                user_id=self.user["id"],
                subscription_id=subscription["id"],
                channel="@ceafamily",
            )
            provisioned = self.trials.mark_status(
                conn,
                claim_id=first["id"],
                status="provisioned",
            )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(provisioned["status"], "provisioned")

    def test_trial_claim_rejects_paid_or_other_users_subscription(self) -> None:
        paid_subscription = self._create_subscription()
        with self.assertRaisesRegex(ValueError, "requires a trial subscription"):
            with self.db.transaction() as conn:
                self.trials.create(
                    conn,
                    user_id=self.user["id"],
                    subscription_id=paid_subscription["id"],
                    channel="@ceafamily",
                )

        now = utcnow().isoformat()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO vpn_trial_claims (
                        user_id, subscription_id, subscription_kind, channel,
                        status, claimed_at, created_at, updated_at
                    )
                    VALUES (?, ?, 'trial', ?, 'pending', ?, ?, ?)
                    """,
                    (
                        self.user["id"],
                        paid_subscription["id"],
                        "@ceafamily",
                        now,
                        now,
                        now,
                    ),
                )

        other_user = UserService(self.db).ensure_telegram_user(
            telegram_id=7002,
            username="vpn_tester_2",
            first_name="VPN 2",
            last_name="Tester",
            language_code="ru",
        )
        other_trial = self._create_subscription(
            provider_username="ceavpn_7002",
            user_id=other_user["id"],
            kind="trial",
        )
        with self.assertRaisesRegex(ValueError, "belongs to another user"):
            with self.db.transaction() as conn:
                self.trials.create(
                    conn,
                    user_id=self.user["id"],
                    subscription_id=other_trial["id"],
                    channel="@ceafamily",
                )

    def test_locally_expired_subscription_remains_due_for_disable(self) -> None:
        subscription = self._create_subscription()
        due_at = (utcnow() + timedelta(days=31)).isoformat()
        with self.db.transaction() as conn:
            self.subscriptions.mark_active(
                conn,
                subscription_id=subscription["id"],
                subscription_url="https://vpn1.example.test/sub/token",
            )
            self.subscriptions.expire_stale_for_user(
                conn,
                user_id=self.user["id"],
                now=due_at,
            )
            expired = self.subscriptions.get_by_id(conn, subscription["id"])
            due = self.subscriptions.list_due_for_expiration(
                conn,
                due_at=due_at,
            )

        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(expired["status"], "expired")
        self.assertEqual([row["id"] for row in due], [subscription["id"]])

    def test_provisioning_jobs_are_idempotent_and_retryable(self) -> None:
        subscription = self._create_subscription()
        current = utcnow()
        past = (current - timedelta(minutes=1)).isoformat()
        future = (current + timedelta(minutes=5)).isoformat()

        with self.db.transaction() as conn:
            first, first_created = self.jobs.enqueue(
                conn,
                subscription_id=subscription["id"],
                operation="create",
                idempotency_key=f"vpn:create:{subscription['id']}",
                next_attempt_at=past,
            )
            second, second_created = self.jobs.enqueue(
                conn,
                subscription_id=subscription["id"],
                operation="create",
                idempotency_key=f"vpn:create:{subscription['id']}",
                next_attempt_at=past,
            )
            due = self.jobs.list_due(conn, due_at=current.isoformat())
            running = self.jobs.claim_due(
                conn,
                due_at=current.isoformat(),
                lease_seconds=60,
                lease_token="worker-one",
            )
            already_claimed = self.jobs.claim_due(
                conn,
                due_at=(current + timedelta(seconds=30)).isoformat(),
                lease_seconds=60,
                lease_token="worker-two-early",
            )
            recovered = self.jobs.claim_due(
                conn,
                due_at=(current + timedelta(seconds=61)).isoformat(),
                lease_seconds=60,
                lease_token="worker-two",
            )
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                self.jobs.mark_completed(
                    conn,
                    job_id=first["id"],
                    lease_token="worker-one",
                )
            failed = self.jobs.mark_failed(
                conn,
                job_id=first["id"],
                lease_token="worker-two",
                error_message="temporary API failure",
                next_attempt_at=future,
            )
            due_before_retry = self.jobs.list_due(
                conn,
                due_at=(current + timedelta(minutes=2)).isoformat(),
            )
            retry = self.jobs.claim_due(
                conn,
                due_at=future,
                lease_seconds=60,
                lease_token="worker-three",
            )
            completed = self.jobs.mark_completed(
                conn,
                job_id=first["id"],
                lease_token="worker-three",
            )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual([job["id"] for job in due], [first["id"]])
        self.assertIsNotNone(running)
        assert running is not None
        self.assertEqual(running["attempts"], 1)
        self.assertEqual(running["lease_token"], "worker-one")
        self.assertIsNone(already_claimed)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(recovered["lease_token"], "worker-two")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["last_error"], "temporary API failure")
        self.assertIsNone(failed["lease_token"])
        self.assertEqual(due_before_retry, [])
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry["attempts"], 3)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["lease_token"])
        self.assertIsNotNone(completed["completed_at"])


if __name__ == "__main__":
    unittest.main()
