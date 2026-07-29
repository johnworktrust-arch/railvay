from __future__ import annotations

import base64
import unittest

from ceai.config import Settings
from ceai.vpn_subscription_delivery import (
    delivery_subscription_url,
    merge_subscription_profiles,
    parse_extra_profiles,
)


class VpnSubscriptionDeliveryTest(unittest.TestCase):
    def test_builds_opaque_railway_subscription_url(self) -> None:
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            app_base_url="https://bot.example.test",
            vpn_delivery_base_url="https://bot.example.test",
            vpn_delivery_signing_secret="s" * 48,
        )
        url = delivery_subscription_url(
            {
                "id": 42,
                "provider_username": "u_abcdef123456",
                "subscription_url": "https://origin.example.test/sub/private",
            },
            settings,
        )
        self.assertRegex(
            url,
            r"^https://bot\.example\.test/sub/42\.[0-9a-f]{64}$",
        )
        self.assertNotIn("private", url)
        self.assertNotIn("u_abcdef", url)

    def test_merges_extra_profile_into_base64_subscription(self) -> None:
        provider_uuid = "9c97ef67-c753-46d0-9529-74a33f566773"
        existing = (
            f"vless://{provider_uuid}@old.example.test:8443"
            "?encryption=none&security=tls&type=ws&path=%2Fws-"
            f"{'1' * 48}#Old\n"
        ).encode()
        encoded = base64.b64encode(existing)
        profiles = parse_extra_profiles(
            "[{"
            '"remark":"⭐ Белые списки · Россия",'
            '"address":"sub.example.test","port":8443,'
            '"sni":"sub.example.test","host":"sub.example.test",'
            f'"path":"/ws-{"2" * 48}"'
            "}]"
        )

        merged = merge_subscription_profiles(
            encoded,
            provider_uuid=provider_uuid,
            profiles=profiles,
        )
        decoded = base64.b64decode(merged).decode()
        self.assertEqual(decoded.count("vless://"), 2)
        self.assertIn("@sub.example.test:8443", decoded)
        self.assertIn("%E2%AD%90%20%D0%91%D0%B5%D0%BB%D1%8B%D0%B5", decoded)

        duplicate = merge_subscription_profiles(
            merged,
            provider_uuid=provider_uuid,
            profiles=profiles,
        )
        self.assertEqual(base64.b64decode(duplicate).decode().count("vless://"), 2)

    def test_rejects_non_ws_profile_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid VPN extra profile"):
            parse_extra_profiles(
                '[{"remark":"Bad","address":"a","port":443,'
                '"sni":"a","host":"a","path":"/"}]'
            )


if __name__ == "__main__":
    unittest.main()
