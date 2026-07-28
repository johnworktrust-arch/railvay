from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ceai.vpn_bot.handlers import (
    connect_landing_url,
    happ_landing_url,
    main_keyboard,
    subscription_screen,
    trial_expiry_reminder_screen,
    v2box_landing_url,
)


class VpnBotUiTest(unittest.TestCase):
    def test_main_menu_hides_trial_after_it_has_been_used(self) -> None:
        available = main_keyboard(
            support_username="cea_help",
            trial_available=True,
        )
        used = main_keyboard(
            support_username="cea_help",
            trial_available=False,
        )

        self.assertEqual(available.inline_keyboard[0][0].text, "🎁 3 дня бесплатно")
        self.assertFalse(
            any(
                button.callback_data == "vpn:trial"
                for row in used.inline_keyboard
                for button in row
            )
        )
        self.assertEqual(used.inline_keyboard[0][0].text, "Подключить VPN 🚀")

    def test_trial_expiry_reminder_shows_time_and_renewal_button(self) -> None:
        now = datetime(2026, 7, 24, 7, 43, tzinfo=timezone.utc)
        text, keyboard = trial_expiry_reminder_screen(
            datetime(2026, 7, 24, 17, 34, tzinfo=timezone.utc),
            now=now,
        )

        self.assertIn("Пробный период скоро закончится", text)
        self.assertIn("9 часов 51 минута", text)
        self.assertIn("24 июля 2026 года, 20:34 (МСК)", text)
        self.assertIn("3 дня бесплатно", text)
        self.assertIn("Устройств: 1", text)
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "vpn:plans",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "vpn:subscription",
        )

    def test_active_subscription_shows_profile_and_setup_guide(self) -> None:
        subscription_url = "https://sub.example.test:8443/sub/secret-token"
        text, keyboard = subscription_screen(
            {
                "status": "active",
                "plan_name": "30 дней",
                "plan_max_devices": 1,
                "server_region": "NL",
                "ends_at": datetime(2026, 8, 23, 19, 35, tzinfo=timezone.utc),
                "subscription_url": subscription_url,
            },
            support_username="cea_help",
            subscription_base_url="https://sub.example.test:8443",
            user={
                "telegram_id": 1625313155,
                "first_name": "bb",
                "username": "bb_user",
            },
            balance_kopecks=500,
        )

        connect_button = keyboard.inline_keyboard[0][0]
        self.assertEqual(
            connect_button.url,
            "https://sub.example.test:8443/connect/secret-token",
        )
        self.assertEqual(connect_button.text, "Подключить VPN 🚀")
        self.assertEqual(len(keyboard.inline_keyboard), 3)
        self.assertEqual(keyboard.inline_keyboard[1][0].text, "🆘 Поддержка")
        self.assertEqual(keyboard.inline_keyboard[2][0].text, "⬅️ Назад")
        self.assertIn("Имя:</b> bb", text)
        self.assertIn("ID:</b> 1625313155", text)
        self.assertIn("Баланс:</b> 5 ₽", text)
        self.assertIn(subscription_url, text)
        self.assertIn("Тариф:</b> 30 дней", text)
        self.assertIn("Лимит устройств:</b> 1", text)
        self.assertNotIn("Локации:", text)
        self.assertNotIn("Нидерланды", text)
        self.assertNotIn("США", text)
        self.assertNotIn("Финляндия", text)
        self.assertIn("23 августа 2026 года, 22:35 (МСК)", text)

    def test_connect_landing_url_uses_the_same_strict_origin_check(self) -> None:
        self.assertEqual(
            connect_landing_url(
                "https://sub.example.test:8443/sub/token_1",
                "https://sub.example.test:8443",
            ),
            "https://sub.example.test:8443/connect/token_1",
        )
        self.assertEqual(
            connect_landing_url(
                "https://evil.example/sub/token",
                "https://sub.example.test:8443",
            ),
            "",
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
