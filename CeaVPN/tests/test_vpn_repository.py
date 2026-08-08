from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path

from ceavpn.database import Database
from ceavpn.repositories.vpn_plans import VpnPlanRepository
from ceavpn.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository
from ceavpn.repositories.vpn_servers import VpnServerRepository
from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceavpn.repositories.vpn_trial_claims import VpnTrialClaimRepository
from ceavpn.services.vpn import (
    MARZBAN_DIRECT_PROFILE_VERSION,
    MARZBAN_WHITELIST_PROFILE_VERSION,
)
from ceavpn.services.users import UserService
from ceavpn.time_utils import utcnow

WORKER_EPOCH = "e" + "1" * 32


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
            server_columns = conn.execute(
                "PRAGMA table_info(vpn_servers)"
            ).fetchall()
            trial_claim_columns = conn.execute(
                "PRAGMA table_info(vpn_trial_claims)"
            ).fetchall()
        self.assertTrue(table_names.issubset({row["name"] for row in rows}))
        self.assertIn(
            "current_profile_version",
            {row["name"] for row in server_columns},
        )
        self.assertIn(
            "current_worker_epoch",
            {row["name"] for row in server_columns},
        )
        self.assertTrue(
            {
                "expired_notice_claimed_at",
                "expired_notice_sent_at",
            }.issubset({row["name"] for row in trial_claim_columns})
        )

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

        for migration_path in (
            Path("migrations/016_vpn_server_profile_version.sql"),
            Path("migrations/postgres/016_vpn_server_profile_version.sql"),
        ):
            source = migration_path.read_text(encoding="utf-8")
            self.assertIn("current_profile_version", source)
            self.assertIn("current_worker_epoch", source)

        for migration_path in (
            Path("migrations/017_vpn_trial_expired_notices.sql"),
            Path("migrations/postgres/017_vpn_trial_expired_notices.sql"),
        ):
            source = migration_path.read_text(encoding="utf-8")
            self.assertIn("expired_notice_claimed_at", source)
            self.assertIn("expired_notice_sent_at", source)

    def test_expired_notice_migration_backfills_only_old_trials(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE vpn_subscriptions (
                id INTEGER PRIMARY KEY,
                ends_at TEXT NOT NULL
            );
            CREATE TABLE vpn_trial_claims (
                id INTEGER PRIMARY KEY,
                subscription_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        now = utcnow()
        conn.executemany(
            "INSERT INTO vpn_subscriptions (id, ends_at) VALUES (?, ?)",
            [
                (1, (now - timedelta(minutes=1)).isoformat()),
                (2, (now + timedelta(minutes=1)).isoformat()),
            ],
        )
        conn.executemany(
            """
            INSERT INTO vpn_trial_claims (id, subscription_id, status)
            VALUES (?, ?, 'provisioned')
            """,
            [(1, 1), (2, 2)],
        )

        conn.executescript(
            Path("migrations/017_vpn_trial_expired_notices.sql").read_text(
                encoding="utf-8"
            )
        )
        rows = conn.execute(
            """
            SELECT id, expired_notice_sent_at
            FROM vpn_trial_claims
            ORDER BY id
            """
        ).fetchall()
        conn.close()

        self.assertIsNotNone(rows[0]["expired_notice_sent_at"])
        self.assertIsNone(rows[1]["expired_notice_sent_at"])

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

    def test_server_upsert_can_deactivate_decommissioned_node(self) -> None:
        with self.db.transaction() as conn:
            updated_server = self.servers.upsert(
                conn,
                code="de-1",
                name="Germany Primary",
                provider="marzban",
                region="DE",
                api_base_url="https://vpn1.example.test",
                is_active=False,
            )
            active_servers = self.servers.list_active(conn)

        self.assertFalse(updated_server["is_active"])
        self.assertEqual(active_servers, [])

    def test_server_upsert_does_not_reactivate_decommissioned_node(self) -> None:
        with self.db.transaction() as conn:
            self.servers.upsert(
                conn,
                code="de-1",
                name="Germany Primary",
                provider="marzban",
                region="DE",
                api_base_url="https://vpn1.example.test",
                is_active=False,
            )
            updated_server = self.servers.upsert(
                conn,
                code="de-1",
                name="Germany Primary Renamed",
                provider="marzban",
                region="DE",
                api_base_url="https://vpn1.example.test",
            )
            active_servers = self.servers.list_active(conn)

        self.assertFalse(updated_server["is_active"])
        self.assertEqual(updated_server["name"], "Germany Primary Renamed")
        self.assertEqual(active_servers, [])

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

    def test_profile_update_candidates_are_active_and_idempotent(self) -> None:
        subscription = self._create_subscription()
        with self.db.transaction() as conn:
            self.subscriptions.mark_active(
                conn,
                subscription_id=subscription["id"],
                subscription_url="https://vpn1.example.test/sub/token",
            )
            candidates = self.subscriptions.list_active_requiring_profile_update(
                conn,
                server_id=subscription["server_id"],
                profile_version="v2",
                worker_epoch="legacy",
            )
            self.jobs.enqueue(
                conn,
                subscription_id=subscription["id"],
                operation="update",
                idempotency_key=(
                    f"vpn:profile:v2:epoch:legacy:{subscription['id']}"
                ),
            )
            already_queued = (
                self.subscriptions.list_active_requiring_profile_update(
                    conn,
                    server_id=subscription["server_id"],
                    profile_version="v2",
                    worker_epoch="legacy",
                )
            )

        self.assertEqual([row["id"] for row in candidates], [subscription["id"]])
        self.assertEqual(already_queued, [])

    def test_delivery_requires_current_completed_whitelist_replica(self) -> None:
        subscription = self._create_subscription()
        current = utcnow()
        with self.db.transaction() as conn:
            self.subscriptions.mark_active(
                conn,
                subscription_id=int(subscription["id"]),
                subscription_url="https://vpn1.example.test/sub/token",
            )
            self.subscriptions.ensure_provider_uuid(
                conn,
                subscription_id=int(subscription["id"]),
            )
            target = self.servers.upsert(
                conn,
                code="ru-wl-1",
                name="Whitelist 1",
                provider="marzban",
                region="RU",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-ru-wl-1",
                subscription_base_url="https://cover.example.test:8443",
            )
            self.servers.mark_healthy(
                conn,
                server_id=int(target["id"]),
                checked_at=current.isoformat(),
                profile_version=MARZBAN_WHITELIST_PROFILE_VERSION,
                worker_epoch=WORKER_EPOCH,
            )
            ready_arguments = {
                "subscription_id": int(subscription["id"]),
                "server_code": "ru-wl-1",
                "profile_version": MARZBAN_WHITELIST_PROFILE_VERSION,
                "healthy_after": (
                    current - timedelta(minutes=2)
                ).isoformat(),
                "active_at": current.isoformat(),
            }
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )
            profile_job, _ = self.jobs.enqueue(
                conn,
                subscription_id=int(subscription["id"]),
                server_id=int(target["id"]),
                operation="update",
                idempotency_key=(
                    f"vpn:replica:{MARZBAN_WHITELIST_PROFILE_VERSION}:"
                    f"epoch:{WORKER_EPOCH}:{subscription['id']}:"
                    f"server:{target['id']}"
                ),
            )
            running_profile = self.jobs.claim_due(
                conn,
                server_id=int(target["id"]),
                lease_token="profile-lease",
            )
            assert running_profile is not None
            self.assertEqual(running_profile["id"], profile_job["id"])
            self.jobs.mark_completed(
                conn,
                job_id=int(profile_job["id"]),
                lease_token="profile-lease",
            )
            self.assertTrue(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )

            replacement_epoch = "e" + "2" * 32
            self.servers.mark_healthy(
                conn,
                server_id=int(target["id"]),
                checked_at=current.isoformat(),
                profile_version=MARZBAN_WHITELIST_PROFILE_VERSION,
                worker_epoch=replacement_epoch,
            )
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )
            replacement_job, _ = self.jobs.enqueue(
                conn,
                subscription_id=int(subscription["id"]),
                server_id=int(target["id"]),
                operation="update",
                idempotency_key=(
                    f"vpn:replica:{MARZBAN_WHITELIST_PROFILE_VERSION}:"
                    f"epoch:{replacement_epoch}:{subscription['id']}:"
                    f"server:{target['id']}"
                ),
            )
            replacement_running = self.jobs.claim_due(
                conn,
                server_id=int(target["id"]),
                lease_token="replacement-lease",
            )
            assert replacement_running is not None
            self.assertEqual(replacement_running["id"], replacement_job["id"])
            self.jobs.mark_completed(
                conn,
                job_id=int(replacement_job["id"]),
                lease_token="replacement-lease",
            )
            self.assertTrue(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )

            self.servers.mark_healthy(
                conn,
                server_id=int(target["id"]),
                checked_at=current.isoformat(),
                profile_version=MARZBAN_DIRECT_PROFILE_VERSION,
                worker_epoch=replacement_epoch,
            )
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )
            self.servers.mark_healthy(
                conn,
                server_id=int(target["id"]),
                checked_at=current.isoformat(),
                profile_version=MARZBAN_WHITELIST_PROFILE_VERSION,
                worker_epoch=replacement_epoch,
            )
            self.assertTrue(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )

            renewal_job, _ = self.jobs.enqueue(
                conn,
                subscription_id=int(subscription["id"]),
                server_id=int(target["id"]),
                operation="update",
                idempotency_key="vpn:payment:123:update:server:"
                f"{target['id']}",
            )
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )
            running_renewal = self.jobs.claim_due(
                conn,
                server_id=int(target["id"]),
                lease_token="renewal-lease",
            )
            assert running_renewal is not None
            self.assertEqual(running_renewal["id"], renewal_job["id"])
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )
            self.jobs.mark_completed(
                conn,
                job_id=int(renewal_job["id"]),
                lease_token="renewal-lease",
            )
            self.assertTrue(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )

            self.servers.set_active(
                conn,
                server_id=int(target["id"]),
                is_active=False,
            )
            self.assertFalse(
                self.subscriptions.has_completed_server_replica(
                    conn,
                    **ready_arguments,
                )
            )

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
