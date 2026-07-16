from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ceai.vpn_bot.handlers import (
    happ_landing_url,
    subscription_screen,
    v2box_landing_url,
)


class VpnBotUiTest(unittest.TestCase):
    def test_active_subscription_opens_happ_with_copy_fallback(self) -> None:
        subscription_url = "https://sub.example.test:8443/sub/secret-token"
        text, keyboard = subscription_screen(
            {
                "status": "active",
                "plan_name": "3 бесплатных дня",
                "server_region": "NL",
                "ends_at": datetime.now(timezone.utc) + timedelta(days=3),
                "subscription_url": subscription_url,
            },
            support_username="cea_help",
            subscription_base_url="https://sub.example.test:8443",
        )

        open_button = keyboard.inline_keyboard[0][0]
        self.assertEqual(
            open_button.url,
            "https://sub.example.test:8443/happ/secret-token",
        )
        self.assertIsNone(open_button.copy_text)

        v2box_button = keyboard.inline_keyboard[1][0]
        self.assertEqual(
            v2box_button.url,
            "https://sub.example.test:8443/v2box/secret-token",
        )
        self.assertIsNone(v2box_button.copy_text)

        copy_button = keyboard.inline_keyboard[2][0]
        self.assertIsNone(copy_button.url)
        self.assertIsNotNone(copy_button.copy_text)
        assert copy_button.copy_text is not None
        self.assertEqual(copy_button.copy_text.text, subscription_url)
        self.assertIn("Открыть в Happ", text)
        self.assertIn("Открыть в V2Box", text)
        self.assertIn("старый импорт без обновлений", text)
        self.assertFalse(
            any(
                button.url == subscription_url
                for row in keyboard.inline_keyboard
                for button in row
            )
        )

    def test_happ_landing_url_only_accepts_a_plain_https_subscription(self) -> None:
        self.assertEqual(
            happ_landing_url(
                "https://sub.example.test:8443/sub/token_1",
                "https://sub.example.test:8443",
            ),
            "https://sub.example.test:8443/happ/token_1",
        )
        self.assertEqual(
            happ_landing_url(
                "http://sub.example.test:8443/sub/token",
                "https://sub.example.test:8443",
            ),
            "",
        )
        rejected = [
            "https://evil.example/sub/token",
            "https://sub.example.test/sub/token",
            "https://user@sub.example.test:8443/sub/token",
            "https://sub.example.test:8443/other/token",
            "https://sub.example.test:8443/sub/%2e%2e",
            "https://sub.example.test:8443/sub/token?next=evil",
            "https://sub.example.test:8443/sub/token#fragment",
            "https://sub.example.test:8443/sub/" + ("a" * 161),
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(
                    happ_landing_url(value, "https://sub.example.test:8443"),
                    "",
                )

    def test_v2box_landing_url_uses_the_same_strict_origin_check(self) -> None:
        self.assertEqual(
            v2box_landing_url(
                "https://sub.example.test:8443/sub/token_1",
                "https://sub.example.test:8443",
            ),
            "https://sub.example.test:8443/v2box/token_1",
        )
        self.assertEqual(
            v2box_landing_url(
                "https://evil.example/sub/token",
                "https://sub.example.test:8443",
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
