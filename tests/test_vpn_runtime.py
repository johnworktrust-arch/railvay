from __future__ import annotations

import hashlib
import hmac
import time
import unittest

from aiohttp import web

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.services.users import UserService
from ceai.services.vpn import VpnService
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

        job = self.vpn.claim_worker_job(worker_id="worker-nl1", lease_seconds=60)
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

    def test_worker_cannot_inject_another_subscription_host(self) -> None:
        self.vpn.claim_trial(
            user_id=int(self.user["id"]),
            channel="@ceafamily",
        )
        job = self.vpn.claim_worker_job(worker_id="worker-nl1", lease_seconds=60)
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
