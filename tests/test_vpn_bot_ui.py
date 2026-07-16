from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ceai.vpn_bot.handlers import subscription_screen


class VpnBotUiTest(unittest.TestCase):
    def test_active_subscription_is_copied_instead_of_opened_as_html(self) -> None:
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
        )

        copy_button = keyboard.inline_keyboard[0][0]
        self.assertIsNone(copy_button.url)
        self.assertIsNotNone(copy_button.copy_text)
        assert copy_button.copy_text is not None
        self.assertEqual(copy_button.copy_text.text, subscription_url)
        self.assertIn("Добавить подписку", text)
        self.assertIn("старый импорт без обновлений", text)
        self.assertFalse(
            any(
                button.url == subscription_url
                for row in keyboard.inline_keyboard
                for button in row
            )
        )


if __name__ == "__main__":
    unittest.main()
