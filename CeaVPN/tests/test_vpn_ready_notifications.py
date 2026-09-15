import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ceavpn.main import notify_vpn_ready


class ReadyNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_six_server_events_send_one_profile(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        services = SimpleNamespace(users=Mock())
        settings = SimpleNamespace(vpn_support_username="support", vpn_subscription_base_url="https://example.test")
        events = [("create", "https://example.test/sub/key"),
                  ("create", ""), ("create", ""), ("create", ""),
                  ("update", ""), ("update", "https://example.test/sub/key")]
        with patch("ceavpn.main.with_delivery_subscription", side_effect=lambda sub, _: sub) as delivery, \
             patch("ceavpn.main.delivery_base_url", return_value="https://example.test"), \
             patch("ceavpn.main.subscription_screen", return_value=("VPN ready", None)):
            for operation, url in events:
                await notify_vpn_ready(bot=bot, services=services, settings=settings,
                    completion=SimpleNamespace(operation=operation, telegram_id=9001,
                        subscription={"id": 1, "subscription_url": url}))
        bot.send_message.assert_awaited_once()
        delivery.assert_called_once()
