import base64
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from ceavpn.config import load_settings
from ceavpn.database import Database
from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceavpn.seed import seed_reference_data
from ceavpn.time_utils import iso_now, utcnow
from ceavpn.vpn_subscription_delivery import (
    delivery_subscription_url,
    expired_subscription_response,
    is_subscription_active,
    register_vpn_subscription_delivery_routes,
)


class TestExpiredVpnSubscriptionLogic(unittest.TestCase):
    def test_is_subscription_active(self) -> None:
        future_iso = (utcnow() + timedelta(days=1)).isoformat()
        past_iso = (utcnow() - timedelta(days=1)).isoformat()

        active_sub = {"status": "active", "ends_at": future_iso}
        expired_sub = {"status": "expired", "ends_at": future_iso}
        past_sub = {"status": "active", "ends_at": past_iso}

        self.assertTrue(is_subscription_active(active_sub))
        self.assertFalse(is_subscription_active(expired_sub))
        self.assertFalse(is_subscription_active(past_sub))
        self.assertFalse(is_subscription_active(None))
        self.assertFalse(is_subscription_active({}))

    def test_expired_subscription_response_format(self) -> None:
        response = expired_subscription_response()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["profile-title"], "CEA VPN (Подписка истекла)")
        self.assertEqual(
            response.headers["subscription-userinfo"],
            "upload=0; download=0; total=0; expire=0",
        )
        body_text = base64.b64decode(response.body).decode("utf-8")
        self.assertIn("vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1", body_text)
        decoded_uri = unquote(body_text)
        self.assertIn("⚠️ Подписка истекла. Продлите в боте", decoded_uri)


class TestExpiredVpnSubscriptionDeliveryHttp(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        seed_reference_data(self.db)
        self.settings = load_settings()

        app = web.Application()
        register_vpn_subscription_delivery_routes(
            app,
            db=self.db,
            settings=self.settings,
        )
        return app

    def tearDown(self) -> None:
        super().tearDown()
        self.db.close()

    @unittest_run_loop
    async def test_expired_subscription_returns_notice_profile(self) -> None:
        repo = VpnSubscriptionRepository()
        from ceavpn.repositories.users import UserRepository
        user_repo = UserRepository()
        past_iso = (utcnow() - timedelta(days=5)).isoformat()
        future_iso = (utcnow() + timedelta(days=25)).isoformat()
        with self.db.transaction() as conn:
            user = user_repo.upsert_telegram_user(conn, telegram_id=999, username="test999")
            user_id = user["id"]
            sub = repo.create_provisioning(
                conn,
                user_id=user_id,
                server_id=1,
                plan_id=None,
                kind="trial",
                provider_username="user999",
                starts_at=past_iso,
                ends_at=future_iso,
            )
            repo.mark_active(
                conn,
                subscription_id=int(sub["id"]),
                subscription_url="https://marzban.test/sub/user999",
            )
            repo.mark_status(conn, subscription_id=int(sub["id"]), status="expired")
            sub = repo.get_by_id(conn, int(sub["id"]))

        sub_url = delivery_subscription_url(sub, self.settings)
        token = sub_url.split("/sub/")[-1]

        resp = await self.client.get(f"/sub/{token}")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("profile-title"), "CEA VPN (Подписка истекла)")
        body = await resp.text()
        decoded = base64.b64decode(body).decode("utf-8")
        self.assertIn("vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1", decoded)
        self.assertIn("⚠️ Подписка истекла. Продлите в боте", unquote(decoded))

    @unittest_run_loop
    async def test_non_existent_or_old_token_returns_expired_notice(self) -> None:
        token = "9999." + "a" * 64
        resp = await self.client.get(f"/sub/{token}")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("profile-title"), "CEA VPN (Подписка истекла)")
        body = await resp.text()
        decoded = base64.b64decode(body).decode("utf-8")
        self.assertIn("vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1", decoded)
        self.assertIn("⚠️ Подписка истекла. Продлите в боте", unquote(decoded))


if __name__ == "__main__":
    unittest.main()
