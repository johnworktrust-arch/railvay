from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ceai.config import Settings, load_settings
from ceai.main import (
    PLATEGA_CALLBACK_MAX_BODY_BYTES,
    register_vpn_platega_routes,
)
from ceai.services.exceptions import BusinessRuleError
from ceai.services.vpn import VpnPaymentVerificationError


class FakeVpnService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def handle_platega_callback(self, *, headers, payload):
        self.calls.append({"headers": headers, "payload": payload})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(processed=True, duplicate=False)


class VpnPlategaSettingsTest(unittest.TestCase):
    def test_platega_settings_are_separate_from_aggregator_provider(self) -> None:
        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "PAYMENT_PROVIDER": "yookassa",
                    "VPN_PAYMENT_PROVIDER": "PLATEGA",
                    "VPN_PLATEGA_MERCHANT_ID": "merchant-123",
                    "VPN_PLATEGA_SECRET": "platega-secret",
                    "VPN_PLATEGA_API_BASE_URL": "app.platega.test/",
                    "VPN_PLATEGA_WEBHOOK_PATH": "vpn/platega/hook",
                    "VPN_PLATEGA_RETURN_PATH": "/vpn/platega/return",
                    "VPN_PLATEGA_FAILED_PATH": "/vpn/platega/failed",
                    "VPN_PLATEGA_REQUEST_TIMEOUT_SECONDS": "17",
                    "VPN_WORKER_HEALTH_MAX_AGE_SECONDS": "75",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.payment_provider, "yookassa")
        self.assertEqual(settings.vpn_payment_provider, "platega")
        self.assertEqual(settings.vpn_platega_merchant_id, "merchant-123")
        self.assertEqual(settings.vpn_platega_secret, "platega-secret")
        self.assertEqual(
            settings.vpn_platega_api_base_url, "https://app.platega.test"
        )
        self.assertEqual(settings.vpn_platega_webhook_path, "vpn/platega/hook")
        self.assertEqual(settings.vpn_platega_return_path, "/vpn/platega/return")
        self.assertEqual(settings.vpn_platega_failed_path, "/vpn/platega/failed")
        self.assertEqual(settings.vpn_platega_request_timeout_seconds, 17)
        self.assertEqual(settings.vpn_worker_health_max_age_seconds, 75)

    def test_platega_defaults_are_safe_and_use_exact_callback_path(self) -> None:
        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test"}, clear=True),
        ):
            settings = load_settings()

        self.assertEqual(settings.vpn_payment_provider, "disabled")
        self.assertEqual(settings.vpn_platega_api_base_url, "https://app.platega.io")
        self.assertEqual(
            settings.vpn_platega_webhook_path,
            "/payments/vpn/platega/webhook",
        )
        self.assertEqual(
            settings.vpn_platega_return_path,
            "/payments/vpn/platega/return",
        )
        self.assertEqual(
            settings.vpn_platega_failed_path,
            "/payments/vpn/platega/failed",
        )
        self.assertEqual(settings.vpn_worker_health_max_age_seconds, 120)


class VpnPlategaHttpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.vpn = FakeVpnService()
        self.settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://payments.example.test",
            vpn_bot_username="ceavpn_bot",
            vpn_platega_merchant_id="merchant-123",
            vpn_platega_secret="secret-456",
        )

    async def _client(
        self,
        settings: Settings | None = None,
    ) -> TestClient:
        selected_settings = settings or self.settings
        app = web.Application()
        app["settings"] = selected_settings
        register_vpn_platega_routes(
            app,
            settings=selected_settings,
            services=SimpleNamespace(vpn=self.vpn),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    @property
    def authentication_headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": "merchant-123",
            "X-Secret": "secret-456",
        }

    async def test_authenticated_callback_delegates_to_vpn_service(self) -> None:
        client = await self._client()
        payload = {
            "id": "payment-1",
            "status": "CONFIRMED",
            "amount": "149.00",
            "currency": "RUB",
        }

        response = await client.post(
            "/payments/vpn/platega/webhook",
            json=payload,
            headers=self.authentication_headers,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True})
        self.assertEqual(len(self.vpn.calls), 1)
        self.assertEqual(self.vpn.calls[0]["payload"], payload)
        self.assertEqual(
            self.vpn.calls[0]["headers"]["X-MerchantId"], "merchant-123"
        )

    async def test_callback_rejects_invalid_auth_before_service(self) -> None:
        client = await self._client()

        response = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
            headers={"X-MerchantId": "merchant-123", "X-Secret": "wrong"},
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(self.vpn.calls, [])

        duplicated = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
            headers=[
                ("X-MerchantId", "merchant-123"),
                ("X-Secret", "secret-456"),
                ("X-Secret", "secret-456"),
            ],
        )
        self.assertEqual(duplicated.status, 401)
        self.assertEqual(self.vpn.calls, [])

    async def test_callback_rejects_invalid_json_and_oversized_body(self) -> None:
        client = await self._client()

        invalid_json = await client.post(
            "/payments/vpn/platega/webhook",
            data=b"{not-json",
            headers=self.authentication_headers,
        )
        oversized = await client.post(
            "/payments/vpn/platega/webhook",
            data=b"x" * (PLATEGA_CALLBACK_MAX_BODY_BYTES + 1),
            headers=self.authentication_headers,
        )

        self.assertEqual(invalid_json.status, 400)
        self.assertEqual(oversized.status, 413)
        self.assertEqual(self.vpn.calls, [])

    async def test_unconfigured_callback_requests_provider_retry(self) -> None:
        client = await self._client(
            replace(self.settings, vpn_platega_merchant_id="", vpn_platega_secret="")
        )

        response = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "300")
        self.assertEqual(self.vpn.calls, [])

    async def test_permanent_rejection_is_400_and_failures_are_retryable(self) -> None:
        client = await self._client()
        self.vpn.error = VpnPaymentVerificationError("amount mismatch")

        rejected = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
            headers=self.authentication_headers,
        )

        self.assertEqual(rejected.status, 400)
        self.vpn.error = BusinessRuleError("VPN server temporarily unavailable")

        business_unavailable = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
            headers=self.authentication_headers,
        )

        self.assertEqual(business_unavailable.status, 503)
        self.assertEqual(business_unavailable.headers["Retry-After"], "300")
        self.vpn.error = RuntimeError("temporary database failure")

        unavailable = await client.post(
            "/payments/vpn/platega/webhook",
            json={"id": "payment-1"},
            headers=self.authentication_headers,
        )

        self.assertEqual(unavailable.status, 503)
        self.assertEqual(unavailable.headers["Retry-After"], "300")

    async def test_return_and_failed_redirect_only_to_configured_vpn_bot(self) -> None:
        client = await self._client()

        returned = await client.get(
            "/payments/vpn/platega/return", allow_redirects=False
        )
        failed = await client.get(
            "/payments/vpn/platega/failed", allow_redirects=False
        )

        self.assertEqual(returned.status, 302)
        self.assertEqual(returned.headers["Location"], "https://t.me/ceavpn_bot")
        self.assertEqual(failed.status, 302)
        self.assertEqual(failed.headers["Location"], "https://t.me/ceavpn_bot")
        self.assertEqual(self.vpn.calls, [])

    async def test_return_rejects_unsafe_bot_username_from_environment(self) -> None:
        client = await self._client(
            replace(self.settings, vpn_bot_username='bad"><script>')
        )

        response = await client.get(
            "/payments/vpn/platega/return", allow_redirects=False
        )

        self.assertEqual(response.headers["Location"], "https://t.me/ceavpn_bot")


if __name__ == "__main__":
    unittest.main()
