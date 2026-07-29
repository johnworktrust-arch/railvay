from __future__ import annotations

import hashlib
import hmac
import time
import unittest
from datetime import timedelta

from aiohttp import web

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.services.exceptions import BusinessRuleError
from ceai.services.users import UserService
from ceai.services.vpn import VpnService, _marzban_profile_version
from ceai.time_utils import utcnow
from ceai.vpn_worker_api import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WORKER_ID_HEADER,
    VpnWorkerAuthenticator,
    _worker_transport_profile,
    canonical_worker_request,
)

DIRECT_PROFILE_VERSION = _marzban_profile_version(
    ("VLESS TCP REALITY", "VLESS WS TLS FALLBACK"),
    "xtls-rprx-vision",
)
WORKER_EPOCH = "e" + "2" * 32


class VpnRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        self.user = UserService(self.db).ensure_telegram_user(
            telegram_id=9001,
            username="vpn_runtime",
            first_name="VPN",
            last_name="Runtime",
            language_code="ru",
        )
        with self.db.transaction() as conn:
            servers = VpnServerRepository()
            server = servers.upsert(
                conn,
                code="nl-1",
                name="Amsterdam 1",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl1",
                subscription_base_url="https://sub.example.test:8443",
            )
            servers.mark_healthy(
                conn,
                server_id=int(server["id"]),
                checked_at=utcnow().isoformat(),
            )
        self.vpn = VpnService(self.db, server_code="nl-1", trial_days=3)

    def test_signed_legacy_direct_profile_defaults_only_to_vision(self) -> None:
        tags, flow, worker_epoch = _worker_transport_profile(
            {
                "inbound_tags": [
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ]
            }
        )
        self.assertEqual(
            tags,
            ["VLESS TCP REALITY", "VLESS WS TLS FALLBACK"],
        )
        self.assertEqual(flow, "xtls-rprx-vision")
        self.assertEqual(worker_epoch, "legacy")

        with self.assertRaises(web.HTTPForbidden):
            _worker_transport_profile(
                {"inbound_tags": ["VLESS XHTTP REALITY"]}
            )
        with self.assertRaises(web.HTTPForbidden):
            _worker_transport_profile(
                {
                    "inbound_tags": [
                        "VLESS WS TLS FALLBACK",
                        "VLESS TCP REALITY",
                    ]
                }
            )

    def test_xhttp_profile_must_explicitly_sign_empty_flow(self) -> None:
        with self.assertRaises(web.HTTPForbidden):
            _worker_transport_profile(
                {
                    "inbound_tags": ["VLESS XHTTP REALITY"],
                    "vless_flow": "",
                }
            )
        tags, flow, worker_epoch = _worker_transport_profile(
            {
                "inbound_tags": ["VLESS XHTTP REALITY"],
                "vless_flow": "",
                "worker_epoch": WORKER_EPOCH,
            }
        )
        self.assertEqual(tags, ["VLESS XHTTP REALITY"])
        self.assertEqual(flow, "")
        self.assertEqual(worker_epoch, WORKER_EPOCH)

    def test_reconciled_requires_idle_epoch_migration_and_no_future_work(
        self,
    ) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        poll_arguments = {
            "worker_id": "worker-nl1",
            "lease_seconds": 60,
            "control_plane_ready": True,
            "worker_inbound_tags": [
                "VLESS TCP REALITY",
                "VLESS WS TLS FALLBACK",
            ],
            "worker_vless_flow": "xtls-rprx-vision",
            "worker_epoch": WORKER_EPOCH,
        }

        create = self.vpn.claim_worker_poll(**poll_arguments)
        self.assertFalse(create.reconciled)
        self.assertIsNotNone(create.job)
        assert create.job is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(create.job["job_id"]),
            lease_token=str(create.job["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/create",
        )

        migration = self.vpn.claim_worker_poll(**poll_arguments)
        self.assertFalse(migration.reconciled)
        self.assertIsNotNone(migration.job)
        assert migration.job is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(migration.job["job_id"]),
            lease_token=str(migration.job["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/migrate",
        )

        reconciled = self.vpn.claim_worker_poll(**poll_arguments)
        self.assertIsNone(reconciled.job)
        self.assertTrue(reconciled.reconciled)

        with self.db.transaction() as conn:
            self.vpn.jobs.enqueue(
                conn,
                subscription_id=int(trial.subscription["id"]),
                server_id=int(trial.subscription["server_id"]),
                operation="update",
                idempotency_key="vpn:test:future-reconciliation-work",
                next_attempt_at=(utcnow() + timedelta(hours=1)).isoformat(),
            )

        blocked = self.vpn.claim_worker_poll(**poll_arguments)
        self.assertIsNone(blocked.job)
        self.assertFalse(blocked.reconciled)

    def tearDown(self) -> None:
        self.db.close()

    def test_trial_is_idempotent_and_worker_activates_it(self) -> None:
        first = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        second = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertTrue(second.trial_already_used)
        self.assertEqual(first.subscription["id"], second.subscription["id"])

        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["operation"], "create")
        self.assertEqual(
            job["marzban_user"]["inbounds"],
            {
                "vless": [
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ]
            },
        )

        completion = self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=job["job_id"],
            lease_token=job["lease_token"],
            subscription_url="https://sub.example.test:8443/sub/secret-token",
        )
        self.assertEqual(completion.telegram_id, 9001)
        self.assertEqual(completion.subscription["status"], "active")
        current = self.vpn.get_current_subscription(int(self.user["id"]))
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["status"], "active")

        with self.db.transaction() as conn:
            claim = conn.execute(
                "SELECT status FROM vpn_trial_claims WHERE user_id = ?",
                (int(self.user["id"]),),
            ).fetchone()
        self.assertEqual(claim["status"], "provisioned")

    def test_trial_expiry_reminder_is_claimed_once_and_can_retry(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert job is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(job["job_id"]),
            lease_token=str(job["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/reminder",
        )
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE vpn_subscriptions
                SET ends_at = ?
                WHERE id = ?
                """,
                (
                    (utcnow() + timedelta(hours=10, minutes=1)).isoformat(),
                    int(trial.subscription["id"]),
                ),
            )
        self.assertEqual(self.vpn.claim_due_trial_expiry_reminders(), [])

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE vpn_subscriptions
                SET ends_at = ?
                WHERE id = ?
                """,
                (
                    (utcnow() + timedelta(hours=9, minutes=50)).isoformat(),
                    int(trial.subscription["id"]),
                ),
            )

        first = self.vpn.claim_due_trial_expiry_reminders()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["telegram_id"], self.user["telegram_id"])
        self.assertEqual(self.vpn.claim_due_trial_expiry_reminders(), [])

        claim_id = int(first[0]["claim_id"])
        self.vpn.release_trial_expiry_reminder(claim_id)
        retried = self.vpn.claim_due_trial_expiry_reminders()
        self.assertEqual([item["claim_id"] for item in retried], [claim_id])

        self.vpn.complete_trial_expiry_reminder(claim_id)
        self.assertEqual(self.vpn.claim_due_trial_expiry_reminders(), [])
        self.assertTrue(self.vpn.has_used_trial(int(self.user["id"])))

    def test_worker_converges_existing_active_subscription_to_profile_v3(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        create = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert create is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(create["job_id"]),
            lease_token=str(create["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/profile-v2",
        )

        migration = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
            worker_inbound_tags=[
                "VLESS TCP REALITY",
                "VLESS WS TLS FALLBACK",
            ],
            worker_vless_flow="xtls-rprx-vision",
        )
        self.assertIsNotNone(migration)
        assert migration is not None
        self.assertEqual(migration["operation"], "update")
        self.assertEqual(
            migration["marzban_user"]["inbounds"],
            {
                "vless": [
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ]
            },
        )

        completion = self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(migration["job_id"]),
            lease_token=str(migration["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/profile-v2",
        )
        self.assertEqual(completion.subscription["subscription_url"], "")
        stored = self.vpn.get_current_subscription(int(self.user["id"]))
        assert stored is not None
        self.assertEqual(
            stored["subscription_url"],
            "https://sub.example.test:8443/sub/profile-v2",
        )

        self.assertIsNone(
            self.vpn.claim_worker_job(
                worker_id="worker-nl1",
                lease_seconds=60,
                control_plane_ready=True,
                worker_inbound_tags=[
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ],
                worker_vless_flow="xtls-rprx-vision",
            )
        )
        with self.db.transaction() as conn:
            profile_jobs = conn.execute(
                """
                SELECT operation, status, idempotency_key
                FROM vpn_provisioning_jobs
                WHERE subscription_id = ?
                  AND idempotency_key LIKE 'vpn:profile:%'
                """,
                (int(trial.subscription["id"]),),
            ).fetchall()
        self.assertEqual(
            [
                (row["operation"], row["status"], row["idempotency_key"])
                for row in profile_jobs
            ],
            [
                (
                    "update",
                    "completed",
                    f"vpn:profile:{DIRECT_PROFILE_VERSION}:epoch:legacy:"
                    f"{trial.subscription['id']}",
                )
            ],
        )

    def test_whitelist_worker_gets_xhttp_profile_without_vision_flow(self) -> None:
        self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
            worker_inbound_tags=["VLESS XHTTP REALITY"],
            worker_vless_flow="",
            worker_epoch=WORKER_EPOCH,
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(
            job["marzban_user"]["inbounds"],
            {"vless": ["VLESS XHTTP REALITY"]},
        )
        self.assertEqual(
            job["marzban_user"]["proxies"]["vless"]["flow"],
            "",
        )

    def test_invalid_worker_pair_neither_creates_nor_claims_profile_job(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        create = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert create is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(create["job_id"]),
            lease_token=str(create["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/legacy",
        )

        self.assertIsNone(
            self.vpn.claim_worker_job(
                worker_id="worker-nl1",
                lease_seconds=60,
                control_plane_ready=True,
            )
        )
        with self.db.transaction() as conn:
            profile_key = (
                f"vpn:profile:{DIRECT_PROFILE_VERSION}:epoch:legacy:"
                f"{trial.subscription['id']}"
            )
            self.assertIsNone(
                conn.execute(
                    """
                    SELECT id FROM vpn_provisioning_jobs
                    WHERE idempotency_key = ?
                    """,
                    (profile_key,),
                ).fetchone()
            )
            self.vpn.jobs.enqueue(
                conn,
                subscription_id=int(trial.subscription["id"]),
                operation="update",
                idempotency_key=profile_key,
            )

        with self.assertRaisesRegex(
            BusinessRuleError, "transport profile"
        ):
            self.vpn.claim_worker_job(
                worker_id="worker-nl1",
                lease_seconds=60,
                control_plane_ready=True,
                worker_inbound_tags=["VLESS TCP REALITY"],
                worker_vless_flow="xtls-rprx-vision",
            )
        with self.db.transaction() as conn:
            profile_job = conn.execute(
                """
                SELECT status FROM vpn_provisioning_jobs
                WHERE idempotency_key = ?
                """,
                (profile_key,),
            ).fetchone()
        self.assertEqual(profile_job["status"], "pending")

    def test_profile_convergence_failures_preserve_active_entitlement(self) -> None:
        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        create = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert create is not None
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(create["job_id"]),
            lease_token=str(create["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/profile-retry",
        )

        migration = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
            worker_inbound_tags=[
                "VLESS TCP REALITY",
                "VLESS WS TLS FALLBACK",
            ],
            worker_vless_flow="xtls-rprx-vision",
        )
        assert migration is not None

        for attempt in range(5):
            self.vpn.fail_worker_job(
                worker_id="worker-nl1",
                job_id=int(migration["job_id"]),
                lease_token=str(migration["lease_token"]),
                error_message="fallback inbound is not ready",
            )
            if attempt == 4:
                break
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE vpn_provisioning_jobs
                    SET next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (
                        (utcnow() - timedelta(seconds=1)).isoformat(),
                        int(migration["job_id"]),
                    ),
                )
            migration = self.vpn.claim_worker_job(
                worker_id="worker-nl1",
                lease_seconds=60,
                control_plane_ready=True,
                worker_inbound_tags=[
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ],
                worker_vless_flow="xtls-rprx-vision",
            )
            assert migration is not None

        stored = self.vpn.get_current_subscription(int(self.user["id"]))
        assert stored is not None
        self.assertEqual(stored["id"], trial.subscription["id"])
        self.assertEqual(stored["status"], "active")
        with self.db.transaction() as conn:
            job = conn.execute(
                """
                SELECT status, attempts
                FROM vpn_provisioning_jobs
                WHERE id = ?
                """,
                (int(migration["job_id"]),),
            ).fetchone()
        self.assertEqual((job["status"], job["attempts"]), ("failed", 5))

    def test_server_upsert_does_not_reactivate_manually_disabled_server(self) -> None:
        repository = VpnServerRepository()
        with self.db.transaction() as conn:
            server = repository.get_by_code(conn, "nl-1")
            assert server is not None
            repository.set_active(
                conn,
                server_id=int(server["id"]),
                is_active=False,
            )
            reseeded = repository.upsert(
                conn,
                code="nl-1",
                name="Amsterdam 1",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl1",
                subscription_base_url="https://sub.example.test:8443",
            )

        self.assertFalse(bool(reseeded["is_active"]))

    def test_checkout_readiness_requires_recent_worker_poll(self) -> None:
        repository = VpnServerRepository()
        cutoff = (utcnow() - timedelta(seconds=120)).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_servers SET last_health_at = NULL WHERE code = ?",
                ("nl-1",),
            )
            self.assertIsNone(
                repository.get_checkout_ready_by_code(
                    conn,
                    code="nl-1",
                    healthy_after=cutoff,
                )
            )
            server = repository.get_by_code(conn, "nl-1")
            assert server is not None
            repository.mark_healthy(
                conn,
                server_id=int(server["id"]),
                checked_at=utcnow().isoformat(),
            )
            ready = repository.get_checkout_ready_by_code(
                conn,
                code="nl-1",
                healthy_after=cutoff,
            )

        self.assertIsNotNone(ready)

    def test_worker_claim_requires_exact_control_plane_readiness(self) -> None:
        self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_servers SET last_health_at = NULL WHERE code = ?",
                ("nl-1",),
            )

        with self.assertRaisesRegex(BusinessRuleError, "control plane"):
            self.vpn.claim_worker_job(
                worker_id="worker-nl1",
                lease_seconds=60,
            )
        for unverified in (False, 1):
            with self.assertRaisesRegex(BusinessRuleError, "control plane"):
                self.vpn.claim_worker_job(
                    worker_id="worker-nl1",
                    lease_seconds=60,
                    control_plane_ready=unverified,  # type: ignore[arg-type]
                )

        with self.db.transaction() as conn:
            server = conn.execute(
                "SELECT last_health_at FROM vpn_servers WHERE code = ?",
                ("nl-1",),
            ).fetchone()
            pending = conn.execute(
                "SELECT status FROM vpn_provisioning_jobs"
            ).fetchone()
        self.assertIsNone(server["last_health_at"])
        self.assertEqual(pending["status"], "pending")

        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        self.assertIsNotNone(job)
        with self.db.transaction() as conn:
            server = conn.execute(
                "SELECT last_health_at FROM vpn_servers WHERE code = ?",
                ("nl-1",),
            ).fetchone()
        self.assertIsNotNone(server["last_health_at"])

    def test_worker_completion_does_not_refresh_transport_health(self) -> None:
        self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
            worker_inbound_tags=[
                "VLESS TCP REALITY",
                "VLESS WS TLS FALLBACK",
            ],
            worker_vless_flow="xtls-rprx-vision",
        )
        assert job is not None
        recorded_health = "2000-01-01T00:00:00+00:00"
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE vpn_servers
                SET last_health_at = ?, current_profile_version = ?
                WHERE code = ?
                """,
                (recorded_health, DIRECT_PROFILE_VERSION, "nl-1"),
            )

        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(job["job_id"]),
            lease_token=str(job["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/main-token",
        )

        with self.db.transaction() as conn:
            server = conn.execute(
                """
                SELECT last_health_at, current_profile_version
                FROM vpn_servers
                WHERE code = ?
                """,
                ("nl-1",),
            ).fetchone()
        self.assertEqual(server["last_health_at"], recorded_health)
        self.assertEqual(
            server["current_profile_version"],
            DIRECT_PROFILE_VERSION,
        )

    def test_trial_does_not_issue_when_worker_health_is_stale(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_servers SET last_health_at = NULL WHERE code = ?",
                ("nl-1",),
            )

        with self.assertRaisesRegex(BusinessRuleError, "недоступен"):
            self.vpn.claim_trial(
                user_id=int(self.user["id"]),
                channel="@ceafamily",
            )

        with self.db.transaction() as conn:
            subscriptions = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_subscriptions"
            ).fetchone()["count"]
            claims = conn.execute(
                "SELECT COUNT(*) AS count FROM vpn_trial_claims"
            ).fetchone()["count"]
        self.assertEqual((subscriptions, claims), (0, 0))

    def test_changing_worker_identity_clears_stale_health(self) -> None:
        repository = VpnServerRepository()
        with self.db.transaction() as conn:
            server = repository.get_by_code(conn, "nl-1")
            assert server is not None
            repository.mark_healthy(
                conn,
                server_id=int(server["id"]),
                checked_at=utcnow().isoformat(),
            )
            updated = repository.upsert(
                conn,
                code="nl-1",
                name="Amsterdam 1",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl2",
                subscription_base_url="https://sub.example.test:8443",
            )

        self.assertIsNone(updated["last_health_at"])

    def test_worker_cannot_inject_another_subscription_host(self) -> None:
        self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        job = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert job is not None
        with self.assertRaisesRegex(Exception, "invalid subscription URL"):
            self.vpn.complete_worker_job(
                worker_id="worker-nl1",
                job_id=job["job_id"],
                lease_token=job["lease_token"],
                subscription_url="https://attacker.example/sub/token",
            )

    def test_worker_hmac_accepts_once_and_rejects_replay(self) -> None:
        secret = "s" * 48
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            vpn_worker_id="worker-nl1",
            vpn_worker_secret=secret,
            vpn_worker_clock_skew_seconds=300,
        )
        authenticator = VpnWorkerAuthenticator(self.db, settings)
        body = b'{"worker_id":"worker-nl1"}'
        timestamp = str(int(time.time()))
        nonce = "nonce-1234567890abcdef"
        canonical = canonical_worker_request(
            method="POST",
            path_query="/internal/vpn/worker/claim",
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        signature = hmac.new(
            secret.encode(), canonical, hashlib.sha256
        ).hexdigest()
        headers = {
            WORKER_ID_HEADER: "worker-nl1",
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature,
        }
        self.assertEqual(
            authenticator.authorize(
                method="POST",
                path_query="/internal/vpn/worker/claim",
                headers=headers,
                body=body,
            ),
            "worker-nl1",
        )
        with self.assertRaises(web.HTTPConflict):
            authenticator.authorize(
                method="POST",
                path_query="/internal/vpn/worker/claim",
                headers=headers,
                body=body,
            )

    def test_worker_hmac_accepts_another_registered_active_worker(self) -> None:
        secret = "s" * 48
        with self.db.transaction() as conn:
            VpnServerRepository().upsert(
                conn,
                code="us-1",
                name="USA",
                provider="marzban",
                region="US",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-us1",
                subscription_base_url="https://sub-us.example.test:8443",
            )
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            vpn_worker_id="worker-nl1",
            vpn_worker_secret=secret,
            vpn_worker_clock_skew_seconds=300,
        )
        authenticator = VpnWorkerAuthenticator(self.db, settings)
        body = b'{"worker_id":"worker-us1"}'
        timestamp = str(int(time.time()))
        nonce = "nonce-us-1234567890abcdef"
        canonical = canonical_worker_request(
            method="POST",
            path_query="/internal/vpn/worker/claim",
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        signature = hmac.new(
            secret.encode(), canonical, hashlib.sha256
        ).hexdigest()
        headers = {
            WORKER_ID_HEADER: "worker-us1",
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature,
        }
        self.assertEqual(
            authenticator.authorize(
                method="POST",
                path_query="/internal/vpn/worker/claim",
                headers=headers,
                body=body,
            ),
            "worker-us1",
        )

    def test_worker_hmac_uses_per_worker_secret_override(self) -> None:
        legacy_secret = "l" * 48
        worker_secret = "w" * 48
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            vpn_worker_id="worker-nl1",
            vpn_worker_secret=legacy_secret,
            vpn_worker_secrets=(("worker-nl1", worker_secret),),
            vpn_worker_clock_skew_seconds=300,
        )
        authenticator = VpnWorkerAuthenticator(self.db, settings)
        body = b'{"worker_id":"worker-nl1"}'
        timestamp = str(int(time.time()))
        nonce = "nonce-worker-override-1234"
        canonical = canonical_worker_request(
            method="POST",
            path_query="/internal/vpn/worker/claim",
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        signature = hmac.new(
            worker_secret.encode(), canonical, hashlib.sha256
        ).hexdigest()
        headers = {
            WORKER_ID_HEADER: "worker-nl1",
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature,
        }
        self.assertEqual(
            authenticator.authorize(
                method="POST",
                path_query="/internal/vpn/worker/claim",
                headers=headers,
                body=body,
            ),
            "worker-nl1",
        )

    def test_one_subscription_is_provisioned_on_every_active_server(self) -> None:
        with self.db.transaction() as conn:
            us_server = VpnServerRepository().upsert(
                conn,
                code="us-1",
                name="USA",
                provider="marzban",
                region="US",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-us1",
                subscription_base_url="https://sub-us.example.test:8443",
            )
            VpnServerRepository().mark_healthy(
                conn, server_id=int(us_server["id"])
            )

        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        canonical = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        replica = self.vpn.claim_worker_job(
            worker_id="worker-us1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert canonical is not None
        assert replica is not None
        canonical_uuid = canonical["marzban_user"]["proxies"]["vless"]["id"]
        replica_uuid = replica["marzban_user"]["proxies"]["vless"]["id"]
        self.assertEqual(canonical_uuid, replica_uuid)

        replica_completion = self.vpn.complete_worker_job(
            worker_id="worker-us1",
            job_id=int(replica["job_id"]),
            lease_token=str(replica["lease_token"]),
            subscription_url="https://sub-us.example.test:8443/sub/us-token",
        )
        self.assertEqual(replica_completion.subscription["subscription_url"], "")

        canonical_completion = self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(canonical["job_id"]),
            lease_token=str(canonical["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/main-token",
        )
        self.assertEqual(
            canonical_completion.subscription["subscription_url"],
            "https://sub.example.test:8443/sub/main-token",
        )
        current = self.vpn.get_current_subscription(int(self.user["id"]))
        assert current is not None
        self.assertEqual(current["id"], trial.subscription["id"])
        self.assertEqual(
            current["subscription_url"],
            "https://sub.example.test:8443/sub/main-token",
        )

        with self.db.transaction() as conn:
            jobs = conn.execute(
                """
                SELECT server_id, status
                FROM vpn_provisioning_jobs
                WHERE subscription_id = ?
                ORDER BY server_id
                """,
                (int(trial.subscription["id"]),),
            ).fetchall()
        self.assertEqual(
            [(int(row["server_id"]), row["status"]) for row in jobs],
            [
                (int(trial.subscription["server_id"]), "completed"),
                (int(us_server["id"]), "completed"),
            ],
        )

    def test_replica_converges_independently_to_whitelist_profile(self) -> None:
        with self.db.transaction() as conn:
            us_server = VpnServerRepository().upsert(
                conn,
                code="us-whitelist",
                name="USA whitelist",
                provider="marzban",
                region="US",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-us-whitelist",
                subscription_base_url="https://sub-us.example.test:8443",
            )
            VpnServerRepository().mark_healthy(
                conn, server_id=int(us_server["id"])
            )

        trial = self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        canonical = self.vpn.claim_worker_job(
            worker_id="worker-nl1",
            lease_seconds=60,
            control_plane_ready=True,
        )
        replica = self.vpn.claim_worker_job(
            worker_id="worker-us-whitelist",
            lease_seconds=60,
            control_plane_ready=True,
        )
        assert canonical is not None
        assert replica is not None
        self.vpn.complete_worker_job(
            worker_id="worker-us-whitelist",
            job_id=int(replica["job_id"]),
            lease_token=str(replica["lease_token"]),
            subscription_url="https://sub-us.example.test:8443/sub/us-token",
        )
        self.vpn.complete_worker_job(
            worker_id="worker-nl1",
            job_id=int(canonical["job_id"]),
            lease_token=str(canonical["lease_token"]),
            subscription_url="https://sub.example.test:8443/sub/main-token",
        )

        whitelist_replica = self.vpn.claim_worker_job(
            worker_id="worker-us-whitelist",
            lease_seconds=60,
            control_plane_ready=True,
            worker_inbound_tags=["VLESS XHTTP REALITY"],
            worker_vless_flow="",
            worker_epoch=WORKER_EPOCH,
        )
        self.assertIsNotNone(whitelist_replica)
        assert whitelist_replica is not None
        self.assertEqual(whitelist_replica["operation"], "update")
        self.assertEqual(
            whitelist_replica["marzban_user"]["inbounds"],
            {"vless": ["VLESS XHTTP REALITY"]},
        )
        self.assertEqual(
            whitelist_replica["marzban_user"]["proxies"]["vless"]["flow"],
            "",
        )

        whitelist_version = _marzban_profile_version(
            ("VLESS XHTTP REALITY",), ""
        )
        with self.db.transaction() as conn:
            job = conn.execute(
                """
                SELECT idempotency_key
                FROM vpn_provisioning_jobs
                WHERE id = ?
                """,
                (int(whitelist_replica["job_id"]),),
            ).fetchone()
            worker_server = conn.execute(
                """
                SELECT current_profile_version, current_worker_epoch
                FROM vpn_servers
                WHERE id = ?
                """,
                (int(us_server["id"]),),
            ).fetchone()
        self.assertEqual(
            job["idempotency_key"],
            f"vpn:replica:{whitelist_version}:epoch:{WORKER_EPOCH}:"
            f"{trial.subscription['id']}:server:{us_server['id']}",
        )
        self.assertEqual(
            worker_server["current_profile_version"],
            whitelist_version,
        )
        self.assertEqual(worker_server["current_worker_epoch"], WORKER_EPOCH)


if __name__ == "__main__":
    unittest.main()
