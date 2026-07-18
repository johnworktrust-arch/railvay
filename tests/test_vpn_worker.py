from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import sys
import unittest
from pathlib import Path
from typing import Any


WORKER_PATH = Path(__file__).resolve().parents[1] / "deploy" / "vpn" / "worker.py"
SPEC = importlib.util.spec_from_file_location("ceavpn_deploy_worker", WORKER_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def config(**overrides: Any):
    values = {
        "worker_id": "cea-vpn-nl1",
        "worker_secret": "s" * 48,
        "railway_base_url": "https://railway.example",
        "subscription_base_url": "https://sub.example:8443",
        "marzban_base_url": "http://127.0.0.1:8000",
        "marzban_username": "worker_admin",
        "marzban_password": "secret-password",
        "inbound_tags": worker.DEFAULT_MARZBAN_INBOUND_TAGS,
    }
    values.update(overrides)
    return worker.WorkerConfig(**values)


def response(status: int, payload: Any = None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return worker.HttpResponse(status=status, body=body, headers={})


class SignedRailwayClientTest(unittest.TestCase):
    def test_request_signature_uses_exact_body_and_path(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return response(204)

        client = worker.SignedRailwayClient(
            config(),
            transport=transport,
            clock=lambda: 1_700_000_000,
            nonce_factory=lambda: "fixed-nonce",
        )
        self.assertIsNone(
            client.claim(
                control_plane_ready=True,
                verified_inbound_tags=worker.DEFAULT_MARZBAN_INBOUND_TAGS,
            )
        )

        request = captured["request"]
        body = request.data
        self.assertIs(
            json.loads(body.decode("utf-8"))["control_plane_ready"],
            True,
        )
        self.assertEqual(
            json.loads(body.decode("utf-8"))["inbound_tags"],
            ["VLESS TCP REALITY", "VLESS WS TLS FALLBACK"],
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        canonical = (
            "POST\n/internal/vpn/worker/claim\n1700000000\nfixed-nonce\n"
            + hashlib.sha256(body).hexdigest()
        ).encode("utf-8")
        expected = hmac.new(b"s" * 48, canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-cea-vpn-signature"], expected)
        self.assertEqual(headers["x-cea-vpn-worker-id"], "cea-vpn-nl1")
        self.assertEqual(captured["timeout"], 15.0)

    def test_configuration_rejects_remote_marzban_and_placeholders(self) -> None:
        base_env = {
            "VPN_WORKER_ID": "nl1",
            "VPN_WORKER_SECRET": "x" * 48,
            "VPN_RAILWAY_BASE_URL": "https://railway.example",
            "VPN_SUBSCRIPTION_BASE_URL": "https://sub.example/sub",
            "MARZBAN_BOT_USERNAME": "worker",
            "MARZBAN_BOT_PASSWORD": "password",
        }
        with self.assertRaises(worker.ConfigurationError):
            worker.WorkerConfig.from_env(
                {**base_env, "MARZBAN_BASE_URL": "https://marzban.example"}
            )
        with self.assertRaises(worker.ConfigurationError):
            worker.WorkerConfig.from_env(
                {**base_env, "VPN_WORKER_SECRET": "replace-with-a-long-random-secret"}
            )

    def test_configuration_prefers_dual_tags_and_accepts_legacy_single_tag(self) -> None:
        base_env = {
            "VPN_WORKER_ID": "nl1",
            "VPN_WORKER_SECRET": "x" * 48,
            "VPN_RAILWAY_BASE_URL": "https://railway.example",
            "VPN_SUBSCRIPTION_BASE_URL": "https://sub.example/sub",
            "MARZBAN_BOT_USERNAME": "worker",
            "MARZBAN_BOT_PASSWORD": "password",
        }
        dual = worker.WorkerConfig.from_env(
            {
                **base_env,
                "MARZBAN_INBOUND_TAGS": (
                    "VLESS TCP REALITY, VLESS WS TLS FALLBACK"
                ),
                "MARZBAN_INBOUND_TAG": "IGNORED LEGACY TAG",
            }
        )
        self.assertEqual(
            dual.inbound_tags,
            ("VLESS TCP REALITY", "VLESS WS TLS FALLBACK"),
        )

        legacy = worker.WorkerConfig.from_env(
            {**base_env, "MARZBAN_INBOUND_TAG": "VLESS TCP REALITY"}
        )
        self.assertEqual(legacy.inbound_tags, ("VLESS TCP REALITY",))

        with self.assertRaisesRegex(
            worker.ConfigurationError, "invalid_marzban_inbound_tags"
        ):
            worker.WorkerConfig.from_env(
                {
                    **base_env,
                    "MARZBAN_INBOUND_TAGS": (
                        "VLESS TCP REALITY,VLESS TCP REALITY"
                    ),
                }
            )


class LocalMarzbanClientTest(unittest.TestCase):
    def test_healthcheck_authenticates_and_accepts_missing_sentinel(self) -> None:
        calls = []

        def transport(request, timeout):
            path = request.full_url.removeprefix("http://127.0.0.1:8000")
            calls.append((request.get_method(), path))
            if path == "/api/admin/token":
                return response(200, {"access_token": "admin-token"})
            headers = {
                key.lower(): value for key, value in request.header_items()
            }
            self.assertEqual(headers["authorization"], "Bearer admin-token")
            if path == "/api/user/cea_worker_healthcheck":
                return response(404, {"detail": "not found"})
            if path == "/api/inbounds":
                return response(
                    200,
                    {
                        "vless": [
                            {"tag": "VLESS TCP REALITY"},
                            {"tag": "VLESS WS TLS FALLBACK"},
                        ]
                    },
                )
            self.fail(f"unexpected healthcheck path {path}")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        self.assertEqual(
            client.healthcheck(), worker.DEFAULT_MARZBAN_INBOUND_TAGS
        )
        self.assertEqual(
            client.healthcheck(), worker.DEFAULT_MARZBAN_INBOUND_TAGS
        )

        self.assertEqual(
            calls,
            [
                ("POST", "/api/admin/token"),
                ("GET", "/api/user/cea_worker_healthcheck"),
                ("GET", "/api/inbounds"),
                ("GET", "/api/user/cea_worker_healthcheck"),
                ("GET", "/api/inbounds"),
            ],
        )

    def test_healthcheck_rejects_missing_configured_inbound(self) -> None:
        def transport(request, timeout):
            path = request.full_url.removeprefix("http://127.0.0.1:8000")
            if path == "/api/admin/token":
                return response(200, {"access_token": "admin-token"})
            if path == "/api/user/cea_worker_healthcheck":
                return response(404, {"detail": "not found"})
            if path == "/api/inbounds":
                return response(
                    200,
                    {"vless": [{"tag": "VLESS TCP REALITY"}]},
                )
            self.fail(f"unexpected healthcheck path {path}")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        with self.assertRaisesRegex(
            worker.WorkerError, "marzban_inbounds_not_ready"
        ):
            client.healthcheck()

    def test_create_uses_both_inbounds(self) -> None:
        def transport(request, timeout):
            method = request.get_method()
            path = request.full_url.removeprefix("http://127.0.0.1:8000")
            if path == "/api/admin/token":
                return response(200, {"access_token": "admin-token"})
            if method == "GET" and path == "/api/user/cea_user_1":
                return response(404, {"detail": "not found"})
            if method == "POST" and path == "/api/user":
                created = json.loads(request.data.decode("utf-8"))
                self.assertEqual(
                    created["inbounds"],
                    {
                        "vless": [
                            "VLESS TCP REALITY",
                            "VLESS WS TLS FALLBACK",
                        ]
                    },
                )
                self.assertEqual(
                    created["proxies"]["vless"]["flow"],
                    "xtls-rprx-vision",
                )
                self.assertTrue(created["proxies"]["vless"]["id"])
                return response(
                    200,
                    {
                        **created,
                        "subscription_url": (
                            "https://sub.example:8443/sub/token-secret"
                        ),
                    },
                )
            self.fail(f"unexpected request {method} {path}")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        user = client.ensure_active(
            username="cea_user_1",
            expire=1_800_000_000,
            subscription_id=42,
        )
        self.assertEqual(
            user["subscription_url"],
            "https://sub.example:8443/sub/token-secret",
        )

    def test_create_conflict_preserves_proxy_and_converges_inbounds(self) -> None:
        calls = []
        existing = {
            "username": "cea_user_1",
            "proxies": {"vless": {"id": "existing-uuid"}},
            "subscription_url": "https://sub.example:8443/sub/token-secret",
            "status": "active",
        }

        def transport(request, timeout):
            method = request.get_method()
            path = request.full_url.removeprefix("http://127.0.0.1:8000")
            calls.append((method, path, request.data))
            if path == "/api/admin/token":
                return response(200, {"access_token": "admin-token"})
            if method == "GET" and path == "/api/user/cea_user_1":
                get_count = sum(
                    1 for item in calls if item[0] == "GET" and item[1] == path
                )
                return response(404 if get_count == 1 else 200, existing)
            if method == "POST" and path == "/api/user":
                return response(409, {"detail": "already exists"})
            if method == "PUT" and path == "/api/user/cea_user_1":
                update = json.loads(request.data.decode("utf-8"))
                self.assertNotIn("proxies", update)
                self.assertNotIn("username", update)
                self.assertEqual(update["expire"], 1_800_000_000)
                self.assertEqual(
                    update["inbounds"],
                    {
                        "vless": [
                            "VLESS TCP REALITY",
                            "VLESS WS TLS FALLBACK",
                        ]
                    },
                )
                return response(200, {**existing, **update})
            self.fail(f"unexpected request {method} {path}")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        user = client.ensure_active(
            username="cea_user_1",
            expire=1_800_000_000,
            subscription_id=42,
        )
        self.assertEqual(
            user["subscription_url"],
            "https://sub.example:8443/sub/token-secret",
        )

    def test_update_preserves_proxy_and_converges_inbounds(self) -> None:
        existing = {
            "username": "cea_user_1",
            "proxies": {"vless": {"id": "existing-uuid"}},
            "inbounds": {"vless": ["VLESS TCP REALITY"]},
            "subscription_url": "https://sub.example:8443/sub/token-secret",
            "status": "active",
        }

        def transport(request, timeout):
            method = request.get_method()
            path = request.full_url.removeprefix("http://127.0.0.1:8000")
            if path == "/api/admin/token":
                return response(200, {"access_token": "admin-token"})
            if method == "GET" and path == "/api/user/cea_user_1":
                return response(200, existing)
            if method == "PUT" and path == "/api/user/cea_user_1":
                update = json.loads(request.data.decode("utf-8"))
                self.assertNotIn("proxies", update)
                self.assertEqual(
                    update["inbounds"],
                    {
                        "vless": [
                            "VLESS TCP REALITY",
                            "VLESS WS TLS FALLBACK",
                        ]
                    },
                )
                return response(200, {**existing, **update})
            self.fail(f"unexpected request {method} {path}")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        updated = client.ensure_active(
            username="cea_user_1",
            expire=1_800_000_000,
            subscription_id=42,
        )

        self.assertEqual(
            updated["proxies"],
            {"vless": {"id": "existing-uuid"}},
        )
        self.assertEqual(
            updated["inbounds"],
            {
                "vless": [
                    "VLESS TCP REALITY",
                    "VLESS WS TLS FALLBACK",
                ]
            },
        )

    def test_disable_missing_user_is_idempotent(self) -> None:
        def transport(request, timeout):
            if request.full_url.endswith("/api/admin/token"):
                return response(200, {"access_token": "admin-token"})
            if request.get_method() == "GET":
                return response(404, {"detail": "not found"})
            self.fail("disable must not update an already-missing user")

        client = worker.LocalMarzbanClient(config(), transport=transport)
        self.assertEqual(client.disable("cea_user_1"), {"status": "disabled"})

    def test_rejects_subscription_url_outside_exact_prefix(self) -> None:
        with self.assertRaisesRegex(worker.WorkerError, "untrusted_subscription_url"):
            worker._validate_subscription_url(
                "https://evil.example/sub/token",
                "https://sub.example:8443/sub/",
            )

    def test_normalizes_relative_marzban_subscription_url(self) -> None:
        self.assertEqual(
            worker._absolute_subscription_url(
                "/sub/token-secret",
                "https://sub.example:8443",
            ),
            "https://sub.example:8443/sub/token-secret",
        )
        with self.assertRaisesRegex(worker.WorkerError, "untrusted_subscription_url"):
            worker._validate_subscription_url(
                "https://sub.example:8443/sub/../admin",
                "https://sub.example:8443/sub/",
            )


class FakeRailway:
    def __init__(self, job):
        self.job = job
        self.results = []
        self.failures = []
        self.claim_readiness = []
        self.claim_inbound_tags = []

    def claim(self, *, control_plane_ready, verified_inbound_tags):
        self.claim_readiness.append(control_plane_ready)
        self.claim_inbound_tags.append(tuple(verified_inbound_tags))
        return self.job

    def report_result(self, payload):
        self.results.append(dict(payload))

    def report_failure(self, payload):
        self.failures.append(dict(payload))


class FakeMarzban:
    def __init__(self):
        self.healthchecks = 0

    def healthcheck(self):
        self.healthchecks += 1
        return worker.DEFAULT_MARZBAN_INBOUND_TAGS

    def ensure_active(self, **kwargs):
        return {
            "status": "active",
            "subscription_url": "https://sub.example:8443/sub/do-not-log-token",
        }

    def disable(self, username):
        return {"status": "disabled"}

    def get_user(self, username):
        return None


class FailingMarzban(FakeMarzban):
    def ensure_active(self, **kwargs):
        raise worker.WorkerError("marzban_http_503")


class FailingHealthcheckMarzban(FakeMarzban):
    def healthcheck(self):
        raise worker.WorkerError("marzban_healthcheck_http_503")


class VpnWorkerTest(unittest.TestCase):
    def test_parses_railway_nested_marzban_payload(self) -> None:
        job = worker.ProvisioningJob.from_payload(
            {
                "job_id": 8,
                "lease_token": "lease-token-at-least-sixteen",
                "operation": "create",
                "marzban_user": {
                    "username": "u_abcdef123456",
                    "status": "active",
                    "expire": 1_800_000_000,
                    "data_limit": 0,
                    "note": "CEA VPN subscription 42",
                    "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                    "inbounds": {
                        "vless": [
                            "VLESS TCP REALITY",
                            "VLESS WS TLS FALLBACK",
                        ]
                    },
                },
                "subscription_base_url": "https://sub.example:8443",
            },
            subscription_base_url="https://sub.example:8443",
        )
        self.assertEqual(job.subscription_id, 42)
        self.assertEqual(job.provider_username, "u_abcdef123456")
        self.assertEqual(job.expire, 1_800_000_000)

    def test_parses_minimal_railway_disable_payload(self) -> None:
        job = worker.ProvisioningJob.from_payload(
            {
                "job_id": 9,
                "lease_token": "lease-token-at-least-sixteen",
                "operation": "disable",
                "marzban_user": {
                    "username": "u_abcdef123456",
                    "status": "disabled",
                },
                "subscription_base_url": "https://sub.example:8443",
            },
            subscription_base_url="https://sub.example:8443",
        )
        self.assertEqual(job.subscription_id, 0)
        self.assertEqual(job.operation, "disable")

    def test_reports_result_without_logging_subscription_url(self) -> None:
        railway = FakeRailway(
            {
                "job_id": 7,
                "lease_token": "lease-token-at-least-sixteen",
                "operation": "create",
                "subscription_id": 42,
                "provider_username": "cea_user_1",
                "expire": 1_800_000_000,
            }
        )
        instance = worker.VpnWorker(
            config(), railway=railway, marzban=FakeMarzban()
        )
        with self.assertLogs("ceavpn.worker", level=logging.INFO) as logs:
            self.assertTrue(instance.run_once())
        self.assertEqual(len(railway.results), 1)
        self.assertEqual(railway.claim_readiness, [True])
        self.assertEqual(
            railway.claim_inbound_tags,
            [worker.DEFAULT_MARZBAN_INBOUND_TAGS],
        )
        self.assertIn("subscription_url", railway.results[0])
        self.assertNotIn("do-not-log-token", "\n".join(logs.output))

    def test_does_not_claim_when_marzban_healthcheck_fails(self) -> None:
        railway = FakeRailway(None)
        instance = worker.VpnWorker(
            config(), railway=railway, marzban=FailingHealthcheckMarzban()
        )

        with self.assertRaisesRegex(
            worker.WorkerError, "marzban_healthcheck_http_503"
        ):
            instance.run_once()

        self.assertEqual(railway.claim_readiness, [])

    def test_reports_sanitized_failure_using_server_contract(self) -> None:
        railway = FakeRailway(
            {
                "job_id": 10,
                "lease_token": "lease-token-at-least-sixteen",
                "operation": "create",
                "marzban_user": {
                    "username": "u_abcdef123456",
                    "status": "active",
                    "expire": 1_800_000_000,
                    "note": "CEA VPN subscription 42",
                },
                "subscription_base_url": "https://sub.example:8443",
            }
        )
        instance = worker.VpnWorker(
            config(), railway=railway, marzban=FailingMarzban()
        )
        with self.assertLogs("ceavpn.worker", level=logging.WARNING):
            self.assertTrue(instance.run_once())
        self.assertEqual(len(railway.failures), 1)
        self.assertEqual(railway.failures[0]["error"], "marzban_http_503")
        self.assertNotIn("subscription_url", railway.failures[0])


if __name__ == "__main__":
    unittest.main()
