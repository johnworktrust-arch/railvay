from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ceavpn.bot.handlers import (
    VPN_MAIN_SCREEN_TEXT,
    connect_landing_url,
    happ_landing_url,
    main_keyboard,
    subscription_screen,
    trial_expired_screen,
    trial_expiry_reminder_screen,
    v2box_landing_url,
)


class VpnBotUiTest(unittest.TestCase):
    def test_public_copy_uses_payment_moderation_safe_wording(self) -> None:
        from ceavpn.main import VPN_BOT_DESCRIPTION, VPN_BOT_SHORT_DESCRIPTION

        public_copy = "\n".join(
            [VPN_MAIN_SCREEN_TEXT, VPN_BOT_DESCRIPTION, VPN_BOT_SHORT_DESCRIPTION]
        )

        self.assertNotIn("заблокированным сайтам", public_copy.lower())
        self.assertIn("стабильное и защищённое соединение", public_copy.lower())
        self.assertLessEqual(len(VPN_BOT_SHORT_DESCRIPTION), 120)
        self.assertLessEqual(len(VPN_BOT_DESCRIPTION), 512)

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

    def test_user_with_existing_subscription_has_used_trial(self) -> None:
        from ceavpn.database import Database
        from ceavpn.config import load_settings
        from ceavpn.services.app import build_services
        from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
        from ceavpn.seed import seed_reference_data

        db = Database("sqlite:///:memory:")
        db.migrate()
        seed_reference_data(db)
        settings = load_settings()
        services = build_services(db, settings)

        user = services.users.ensure_telegram_user(telegram_id=5555, username="sub_user")
        user_id = int(user["id"])

        self.assertFalse(services.vpn.has_used_trial(user_id))

        repo = VpnSubscriptionRepository()
        with db.transaction() as conn:
            sub = repo.create_provisioning(
                conn,
                user_id=user_id,
                server_id=1,
                plan_id=1,
                kind="paid",
                provider_username="u_5555",
                starts_at="2026-01-01T00:00:00Z",
                ends_at="2026-02-01T00:00:00Z",
            )
            repo.mark_active(conn, subscription_id=int(sub["id"]), subscription_url="https://sub.test/1")
            repo.mark_status(conn, subscription_id=int(sub["id"]), status="expired")

        # Now the user has a past expired subscription, so has_used_trial should be True
        self.assertTrue(services.vpn.has_used_trial(user_id))
        db.close()

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
        self.assertIn("Устройств: 2", text)
        self.assertTrue(text.startswith("<b>Пробный период скоро закончится</b>\n⚠️"))
        self.assertEqual(
            keyboard.inline_keyboard[0][0].text,
            "🔄 Продлить подписку",
        )
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "vpn:plans",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].text,
            "👤 Моя подписка",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "vpn:subscription",
        )

    def test_trial_expired_screen_shows_only_renewal_button(self) -> None:
        text, keyboard = trial_expired_screen(
            datetime(2026, 7, 24, 17, 34, tzinfo=timezone.utc)
        )

        self.assertTrue(text.startswith("<b>Пробный период закончился</b>\n⚠️"))
        self.assertIn("Пробный период завершён", text)
        self.assertIn("24 июля 2026 года, 20:34 (МСК)", text)
        self.assertIn("3 дня бесплатно", text)
        self.assertIn("Устройств: 2", text)
        self.assertNotIn("Осталось времени", text)
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(len(keyboard.inline_keyboard[0]), 1)
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "🔄 Продлить подписку")
        self.assertEqual(button.callback_data, "vpn:plans")
        self.assertEqual(button.style, "success")

    def test_active_subscription_shows_profile_and_setup_guide(self) -> None:
        subscription_url = "https://sub.example.test:8443/sub/secret-token"
        text, keyboard = subscription_screen(
            {
                "status": "active",
                "plan_name": "30 дней",
                "plan_max_devices": 2,
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
        self.assertEqual(len(keyboard.inline_keyboard), 4)
        self.assertEqual(keyboard.inline_keyboard[1][0].text, "🔄 Продлить подписку")
        self.assertEqual(keyboard.inline_keyboard[2][0].text, "🆘 Поддержка")
        self.assertEqual(keyboard.inline_keyboard[3][0].text, "⬅️ Назад")
        self.assertIn("Имя: bb", text)
        self.assertIn("ID: 1625313155", text)
        self.assertIn("Тариф: 30 дней", text)
        self.assertIn("Лимит устройств: 2", text)
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
