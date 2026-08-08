from __future__ import annotations

import base64
import importlib.util
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


PROXY_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "vpn"
    / "subscription_proxy.py"
)
SPEC = importlib.util.spec_from_file_location(
    "ceavpn_subscription_proxy",
    PROXY_PATH,
)
assert SPEC is not None and SPEC.loader is not None
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)


class VpnSubscriptionProxyTest(unittest.TestCase):
    def test_expiration_is_read_from_subscription_userinfo(self) -> None:
        headers = {
            "subscription-userinfo": (
                "upload=0; download=0; total=0; expire=1700000000"
            )
        }

        self.assertEqual(proxy.expiration_from_headers(headers), 1_700_000_000)
        self.assertFalse(proxy.subscription_has_expired(headers, now=1_699_999_999))
        self.assertTrue(proxy.subscription_has_expired(headers, now=1_700_000_000))

    def test_missing_or_unlimited_expiration_is_not_treated_as_expired(self) -> None:
        self.assertFalse(proxy.subscription_has_expired({}, now=2_000_000_000))
        self.assertFalse(
            proxy.subscription_has_expired(
                {"subscription-userinfo": "expire=0"},
                now=2_000_000_000,
            )
        )

    def test_expired_body_contains_only_status_and_renewal_profiles(self) -> None:
        body = proxy.expired_subscription_body("ceavpn_bot")
        links = base64.b64decode(body).decode("utf-8").splitlines()
        remarks = [unquote(urlsplit(link).fragment) for link in links]

        self.assertEqual(
            remarks,
            [
                "🔴 Подписка истекла",
                "👉 Продли в боте @ceavpn_bot",
            ],
        )
        self.assertTrue(all(link.startswith("vless://") for link in links))
        self.assertTrue(all("@127.0.0.1:" in link for link in links))

    def test_device_limit_exceeded_body_contains_correct_remarks(self) -> None:
        body = proxy.device_limit_exceeded_body("ceavpn_bot")
        links = base64.b64decode(body).decode("utf-8").splitlines()
        remarks = [unquote(urlsplit(link).fragment) for link in links]

        self.assertEqual(
            remarks,
            [
                "🔴 Лимит устройств исчерпан",
                "👉 Докупить устройства можно в боте @ceavpn_bot",
            ],
        )

    def test_is_device_limit_exceeded_enforces_max_devices(self) -> None:
        path = "/sub/test_token_123"
        proxy.DEVICES_REGISTRY.clear()

        # Device 1 and 2 allowed for max_devices = 2
        self.assertFalse(proxy.is_device_limit_exceeded(path, "ip1:agent1", 2, now=100))
        self.assertFalse(proxy.is_device_limit_exceeded(path, "ip2:agent2", 2, now=100))

        # Device 3 exceeds limit of 2
        self.assertTrue(proxy.is_device_limit_exceeded(path, "ip3:agent3", 2, now=100))

        # Increasing max_devices to 3 allows Device 3
        self.assertFalse(proxy.is_device_limit_exceeded(path, "ip3:agent3", 3, now=100))

    def test_bot_username_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            proxy.expired_subscription_body("bad username")


if __name__ == "__main__":
    unittest.main()
