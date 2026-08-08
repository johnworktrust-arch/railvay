from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ceavpn.config import (
    DEFAULT_VPN_PRIVACY_POLICY_URL,
    DEFAULT_VPN_USER_AGREEMENT_URL,
    Settings,
    load_settings,
)
from ceavpn.health import _handle_health_request
from ceavpn.main import vpn_user_agreement
from ceavpn.bot.handlers import about_keyboard, payment_keyboard
from ceavpn.vpn_user_agreement import render_vpn_user_agreement_html


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "ai-token",
        "database_url": "sqlite:///:memory:",
        "app_env": "test",
        "mock_payment_base_url": "https://payments.example.test",
        "vpn_bot_username": "ceavpn_bot",
        "vpn_support_username": "cea_help",
        "vpn_user_agreement_url": "https://vpn.example.test/vpn/user-agreement",
        "vpn_privacy_policy_url": "https://vpn.example.test/vpn/privacy-policy",
    }
    values.update(overrides)
    return Settings(**values)


class VpnUserAgreementRenderTest(unittest.TestCase):
    def test_agreement_contains_actual_vpn_terms_without_owner_details(self) -> None:
        settings = _settings(vpn_trial_days=3)

        html = render_vpn_user_agreement_html(settings)

        self.assertIn("Пользовательское соглашение CEA VPN", html)
        self.assertIn("Актуальная редакция", html)
        self.assertIn("одного устройства", html)
        self.assertIn("3 календарных дня", html)
        self.assertIn("Автоматическое продление и рекуррентные списания не применяются", html)
        self.assertIn("Platega", html)
        self.assertIn("фактически оказанной части услуги", html)
        self.assertIn("не обещает абсолютную анонимность", html)
        self.assertIn("Ежедневно, с 08:00 до 22:00 МСК", html)
        self.assertNotIn("<dt>ИНН</dt>", html)
        self.assertNotIn("<dt>ОГРН", html)
        self.assertNotIn("<dt>Адрес</dt>", html)
        self.assertNotIn("Электронная почта", html)

    def test_support_hours_are_configurable_and_escaped(self) -> None:
        html = render_vpn_user_agreement_html(
            _settings(vpn_support_hours='08:00–22:00 МСК <script>alert("x")</script>')
        )

        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert", html)


class VpnUserAgreementConfigTest(unittest.TestCase):
    def test_railway_domain_keeps_separate_telegraph_vpn_documents(self) -> None:
        with (
            patch("ceavpn.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "RAILWAY_PUBLIC_DOMAIN": "cea-ai-production.up.railway.app",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.vpn_user_agreement_url, DEFAULT_VPN_USER_AGREEMENT_URL)
        self.assertEqual(settings.vpn_privacy_policy_url, DEFAULT_VPN_PRIVACY_POLICY_URL)
        self.assertNotEqual(settings.vpn_user_agreement_url, settings.public_offer_url)

    def test_empty_overrides_do_not_hide_vpn_documents(self) -> None:
        with (
            patch(
                "ceavpn.config._load_dotenv",
                return_value={
                    "VPN_USER_AGREEMENT_URL": "",
                    "VPN_PRIVACY_POLICY_URL": "",
                },
            ),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "RAILWAY_PUBLIC_DOMAIN": "cea-ai-production.up.railway.app",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.vpn_user_agreement_url, DEFAULT_VPN_USER_AGREEMENT_URL)
        self.assertEqual(settings.vpn_privacy_policy_url, DEFAULT_VPN_PRIVACY_POLICY_URL)

    def test_explicit_vpn_document_urls_and_support_hours_are_read(self) -> None:
        with (
            patch("ceavpn.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "VPN_USER_AGREEMENT_URL": "https://telegra.ph/vpn-offer-test",
                    "VPN_PRIVACY_POLICY_URL": "https://telegra.ph/vpn-privacy-test",
                    "VPN_SUPPORT_HOURS": "08:00–22:00 МСК",
                    "VPN_AGREEMENT_VERSION": "2.1",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.vpn_user_agreement_url, "https://telegra.ph/vpn-offer-test")
        self.assertEqual(settings.vpn_privacy_policy_url, "https://telegra.ph/vpn-privacy-test")
        self.assertEqual(settings.vpn_support_hours, "08:00–22:00 МСК")
        self.assertEqual(settings.vpn_agreement_version, "2.1")

    def test_non_telegraph_vpn_document_overrides_fall_back_to_defaults(self) -> None:
        with (
            patch("ceavpn.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "VPN_USER_AGREEMENT_URL": "https://telegra.ph:bad/offer",
                    "VPN_PRIVACY_POLICY_URL": "https://example.com/privacy",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.vpn_user_agreement_url, DEFAULT_VPN_USER_AGREEMENT_URL)
        self.assertEqual(settings.vpn_privacy_policy_url, DEFAULT_VPN_PRIVACY_POLICY_URL)


class VpnAboutKeyboardTest(unittest.TestCase):
    def test_about_uses_only_vpn_document_urls(self) -> None:
        settings = _settings(public_offer_url="https://ai.example/offer")

        keyboard = about_keyboard(settings)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        by_text = {button.text: button for button in buttons}

        self.assertEqual(
            by_text["📄 Пользовательское соглашение"].url,
            "https://vpn.example.test/vpn/user-agreement",
        )
        self.assertEqual(
            by_text["🔒 Политика конфиденциальности"].url,
            "https://vpn.example.test/vpn/privacy-policy",
        )
        self.assertNotIn("https://ai.example/offer", [button.url for button in buttons])
        self.assertEqual(
            [len(row) for row in keyboard.inline_keyboard],
            [2, 1, 1, 1],
        )
        self.assertEqual(
            [button.text for button in keyboard.inline_keyboard[0]],
            [
                "📄 Пользовательское соглашение",
                "🔒 Политика конфиденциальности",
            ],
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "vpn:promo",
        )
        self.assertEqual(
            keyboard.inline_keyboard[2][0].url,
            "https://t.me/cea_help",
        )
        self.assertEqual(
            keyboard.inline_keyboard[3][0].callback_data,
            "vpn:main",
        )

    def test_payment_selection_repeats_agreement_link_before_checkout(self) -> None:
        keyboard = payment_keyboard(
            "1", "https://vpn.example.test/vpn/user-agreement"
        )
        buttons = [button for row in keyboard.inline_keyboard for button in row]

        agreement_buttons = [
            button
            for button in buttons
            if button.text == "📄 Пользовательское соглашение"
        ]
        self.assertEqual(len(agreement_buttons), 1)
        self.assertEqual(
            agreement_buttons[0].url,
            "https://vpn.example.test/vpn/user-agreement",
        )


class VpnUserAgreementHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_aiohttp_route_returns_mobile_html_and_security_headers(self) -> None:
        app = web.Application()
        app["settings"] = _settings()
        app.router.add_get("/vpn/user-agreement", vpn_user_agreement)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        response = await client.get("/vpn/user-agreement")
        body = await response.text()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertEqual(response.charset, "utf-8")
        self.assertIn('name="viewport"', body)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    async def test_polling_health_server_serves_same_agreement_path(self) -> None:
        settings = _settings(vpn_trial_days=5)
        server = await asyncio.start_server(
            lambda reader, writer: _handle_health_request(
                reader, writer, settings, None
            ),
            host="127.0.0.1",
            port=0,
        )

        async def close_server() -> None:
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        port = int(server.sockets[0].getsockname()[1])

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /vpn/user-agreement HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

        self.assertIn(b"HTTP/1.1 200 OK", response)
        self.assertIn(b"Content-Type: text/html; charset=utf-8", response)
        self.assertIn("5 календарных дней".encode("utf-8"), response)


if __name__ == "__main__":
    unittest.main()
