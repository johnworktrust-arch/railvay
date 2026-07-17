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
from ceai.services.vpn import VpnService
from ceai.time_utils import utcnow
from ceai.vpn_worker_api import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WORKER_ID_HEADER,
    VpnWorkerAuthenticator,
    canonical_worker_request,
)


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
            {"vless": ["VLESS TCP REALITY"]},
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


if __name__ == "__main__":
    unittest.main()
