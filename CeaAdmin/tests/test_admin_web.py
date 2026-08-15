from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from ceaadmin.admin_web import (
    SERVICES_KEY,
    _session_token,
    _valid_session,
    create_admin_app,
)
from ceaadmin.config import Settings
from ceaadmin.database import Database
from ceaadmin.repositories.plans import PlanRepository
from ceaadmin.repositories.vpn_servers import VpnServerRepository
from ceaadmin.seed import seed_reference_data
from ceaadmin.services.app import build_services
from ceaadmin.time_utils import utcnow


class AdminWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        seed_reference_data(self.db)
        with self.db.transaction() as conn:
            PlanRepository().upsert(
                conn,
                code="start",
                name="Start",
                price_rub=299,
                duration_days=30,
                coins_amount=100,
                features={},
            )
        self.settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            vpn_telegram_bot_token="vpn-test",
            admin_telegram_ids=(9001,),
            vpn_subscription_base_url="https://sub.example.test:8443",
        )
        services = build_services(self.db, self.settings)
        self.owner = services.users.ensure_telegram_user(
            telegram_id=9001,
            username="owner",
            first_name="Owner",
        )
        self.trial_user = services.users.ensure_telegram_user(
            telegram_id=9101,
            username="trial",
            first_name="Trial",
        )
        services.subscriptions.grant_channel_gift(
            self.trial_user["id"],
            plan_code="start",
            duration_days=30,
            coins_amount=5,
            gift_key="ceafamily",
        )
        with self.db.transaction() as conn:
            servers = VpnServerRepository()
            server = servers.upsert(
                conn,
                code="nl-1",
                name="Amsterdam 1",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="cea-vpn-nl1",
                subscription_base_url="https://sub.example.test:8443",
            )
            servers.mark_healthy(
                conn,
                server_id=int(server["id"]),
                checked_at=utcnow().isoformat(),
            )
        services.vpn.claim_trial(
            user_id=self.trial_user["id"],
            channel="@ceafamily",
        )
        self.paid_user = services.users.ensure_telegram_user(
            telegram_id=9201,
            username="paid",
            first_name="Paid",
        )
        payment = services.payments.create_mock_payment(
            user_id=self.paid_user["id"], plan_code="start"
        )
        services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE payments SET provider = 'platega' WHERE id = ?",
                (payment["id"],),
            )
        self.mock_user = services.users.ensure_telegram_user(
            telegram_id=9301,
            username="mock-paid",
            first_name="Mock",
        )
        mock_payment = services.payments.create_mock_payment(
            user_id=self.mock_user["id"], plan_code="start"
        )
        services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=mock_payment["id"]
        )

        app = create_admin_app(
            db=self.db,
            settings=self.settings,
            admin_token="admin-test-token",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"X-Cea-Admin-Token": "admin-test-token"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.db.close()

    async def test_dashboard_reports_trial_paid_and_registration_data(self) -> None:
        stats_response = await self.client.get("/api/stats", headers=self.headers)
        self.assertEqual(stats_response.status, 200)
        stats = await stats_response.json()
        self.assertEqual(stats["users_total"], 3)
        self.assertEqual(stats["admin_users"], 1)
        self.assertEqual(stats["trial_users"], 1)
        self.assertEqual(stats["paid_users"], 1)
        self.assertEqual(stats["active_subscriptions"], 3)
        self.assertEqual(stats["paid_payments"], 1)
        self.assertEqual(stats["platega_paid_payments"], 1)
        self.assertEqual(stats["mock_paid_payments"], 1)
        self.assertEqual(stats["revenue_rub"], 299)
        self.assertEqual(stats["conversion_percent"], 33.3)

        users_response = await self.client.get(
            "/api/users?segment=all", headers=self.headers
        )
        self.assertEqual(users_response.status, 200)
        users = await users_response.json()
        self.assertEqual(users["total"], 4)
        paid = next(user for user in users["users"] if user["username"] == "paid")
        trial = next(user for user in users["users"] if user["username"] == "trial")
        mock = next(user for user in users["users"] if user["username"] == "mock-paid")
        self.assertTrue(paid["created_at"])
        self.assertTrue(paid["last_seen_at"])
        self.assertTrue(paid["has_paid"])
        self.assertTrue(trial["has_trial"])
        self.assertFalse(mock["has_paid"])

        filtered_response = await self.client.get(
            "/api/users?segment=paid&q=paid", headers=self.headers
        )
        filtered = await filtered_response.json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["users"][0]["username"], "paid")

    async def test_admin_actions_require_token_and_update_user(self) -> None:
        unauthorized = await self.client.get("/api/stats")
        self.assertEqual(unauthorized.status, 401)

        blocked_response = await self.client.post(
            f"/api/users/{self.paid_user['id']}/blocked",
            headers=self.headers,
            json={"blocked": True},
        )
        self.assertEqual(blocked_response.status, 200)
        blocked = await blocked_response.json()
        self.assertTrue(blocked["user"]["is_blocked"])

        credit_response = await self.client.post(
            f"/api/users/{self.paid_user['id']}/credit",
            headers=self.headers,
            json={"amount": 10},
        )
        self.assertEqual(credit_response.status, 200)
        credited = await credit_response.json()
        self.assertEqual(credited["balance"], 35)

        maintenance_response = await self.client.post(
            "/api/maintenance",
            headers=self.headers,
            json={"active": True},
        )
        self.assertEqual(maintenance_response.status, 200)
        maintenance = await maintenance_response.json()
        self.assertTrue(maintenance["maintenance_active"])

    async def test_message_can_be_sent_to_multiple_selected_users_with_button(self) -> None:
        vpn_user = self.client.server.app[SERVICES_KEY].users.ensure_telegram_user(
            telegram_id=9401,
            username="vpn-message",
            first_name="VPN Message",
        )
        self.client.server.app[SERVICES_KEY].vpn.claim_trial(
            user_id=vpn_user["id"],
            channel="@ceafamily",
        )
        with patch(
            "ceaadmin.admin_web._send_telegram_message",
            return_value=True,
        ) as send:
            response = await self.client.post(
                "/api/vpn/messages",
                headers=self.headers,
                json={
                    "user_ids": [self.trial_user["id"], vpn_user["id"]],
                    "text": "Важное обновление",
                    "button_text": "Открыть",
                    "button_url": "https://example.test/news",
                },
            )
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(send.call_count, 2)

    async def test_message_requires_complete_button_and_selected_users(self) -> None:
        response = await self.client.post(
            "/api/vpn/messages",
            headers=self.headers,
            json={"user_ids": [], "text": "Тест"},
        )
        self.assertEqual(response.status, 400)

        response = await self.client.post(
            "/api/vpn/messages",
            headers=self.headers,
            json={
                "user_ids": [self.trial_user["id"]],
                "text": "Тест",
                "button_text": "Открыть",
            },
        )
        self.assertEqual(response.status, 400)

    async def test_vpn_message_recipients_returns_selectable_list(self) -> None:
        response = await self.client.get(
            "/api/vpn/message-recipients",
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(len(result["users"]), 1)
        self.assertEqual(result["users"][0]["id"], self.trial_user["id"])

    async def test_vpn_promocode_validates_discount_and_resolves_telegram_target(self) -> None:
        invalid = await self.client.post(
            "/api/vpn/promocodes",
            headers=self.headers,
            json={
                "code": "TOO-MUCH",
                "reward_type": "discount_percent",
                "reward_value": 100,
            },
        )
        self.assertEqual(invalid.status, 400)

        created_response = await self.client.post(
            "/api/vpn/promocodes",
            headers=self.headers,
            json={
                "code": "VPN-TEST",
                "reward_type": "days",
                "reward_value": 7,
                "target_user_id": self.trial_user["telegram_id"],
                "expires_at": "2030-01-02",
            },
        )
        self.assertEqual(created_response.status, 200)
        promocode = (await created_response.json())["promocode"]
        self.assertEqual(promocode["target_user_id"], self.trial_user["id"])
        self.assertTrue(str(promocode["expires_at"]).startswith("2030-01-02T23:59:59"))

        missing_target = await self.client.post(
            "/api/vpn/promocodes",
            headers=self.headers,
            json={
                "code": "UNKNOWN-USER",
                "reward_type": "days",
                "reward_value": 7,
                "target_user_id": 999999999,
            },
        )
        self.assertEqual(missing_target.status, 400)

    async def test_vpn_section_reports_subscriptions_servers_and_users(self) -> None:
        stats_response = await self.client.get(
            "/api/vpn/stats",
            headers=self.headers,
        )
        self.assertEqual(stats_response.status, 200)
        stats = await stats_response.json()
        self.assertEqual(stats["users_total"], 1)
        self.assertEqual(stats["trial_users"], 1)
        self.assertEqual(stats["provisioning_subscriptions"], 1)
        self.assertGreaterEqual(stats["servers_total"], 1)
        self.assertIsInstance(stats["servers"], list)

        users_response = await self.client.get(
            "/api/vpn/users?segment=trial&q=trial",
            headers=self.headers,
        )
        self.assertEqual(users_response.status, 200)
        users = await users_response.json()
        self.assertEqual(users["total"], 1)
        vpn_user = users["users"][0]
        self.assertEqual(vpn_user["username"], "trial")
        self.assertTrue(vpn_user["vpn_has_trial"])

        card_response = await self.client.get(
            f"/api/vpn/users/{self.trial_user['id']}",
            headers=self.headers,
        )
        self.assertEqual(card_response.status, 200)
        card = await card_response.json()
        self.assertEqual(card["subscription"]["kind"], "trial")
        self.assertIsNotNone(card["trial"])

    async def test_index_is_local_dashboard_and_never_exposes_api_without_token(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertIn("Cea AI · Админка", text)
        self.assertIn("Cea VPN", text)
        self.assertIn('data-product="vpn"', text)
        self.assertIn('id="vpn-overview-view"', text)
        self.assertIn("<th>Регистрация</th>", text)
        self.assertIn("<th>Последняя активность</th>", text)
        self.assertIn("admin-test-token", text)

    async def test_read_request_reconnects_once_after_database_disconnect(self) -> None:
        admin = self.client.server.app[SERVICES_KEY].admin
        dashboard_stats = admin.dashboard_stats
        attempts = 0

        def flaky_dashboard_stats():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("database connection dropped")
            return dashboard_stats()

        with (
            patch.object(admin, "dashboard_stats", side_effect=flaky_dashboard_stats),
            patch.object(
                self.db,
                "is_connection_error",
                side_effect=lambda exc: isinstance(exc, ConnectionError),
            ),
            patch.object(self.db, "reconnect") as reconnect,
        ):
            response = await self.client.get("/api/stats", headers=self.headers)

        self.assertEqual(response.status, 200)
        self.assertEqual(attempts, 2)
        reconnect.assert_called_once_with()

    async def test_remote_dashboard_requires_password_for_page_assets_and_api(
        self,
    ) -> None:
        password = "owner-password-with-24-chars"
        remote_settings = replace(
            self.settings,
            admin_web_password=password,
            admin_web_session_secret="s" * 48,
        )
        app = create_admin_app(
            db=self.db,
            settings=remote_settings,
            admin_token="remote-admin-token",
            require_login=True,
            secure_cookies=False,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            health = await client.get("/healthz")
            self.assertEqual(health.status, 200)

            root = await client.get("/", allow_redirects=False)
            self.assertEqual(root.status, 200)
            root_text = await root.text()
            self.assertIn("Вход в админку", root_text)

            protected_asset = await client.get(
                "/assets/app.js",
                allow_redirects=False,
            )
            self.assertEqual(protected_asset.status, 302)

            api = await client.get(
                "/api/stats",
                headers={"X-Cea-Admin-Token": "remote-admin-token"},
            )
            self.assertEqual(api.status, 401)

            login_page = await client.get("/login")
            self.assertEqual(login_page.status, 200)
            login_text = await login_page.text()
            self.assertNotIn("remote-admin-token", login_text)
            self.assertIn("Вход в админку", login_text)

            wrong = await client.post(
                "/login",
                data={"password": "wrong-password-that-is-long"},
                allow_redirects=False,
            )
            self.assertEqual(wrong.status, 401)

            authenticated = await client.post(
                "/login",
                data={"password": password},
                allow_redirects=False,
            )
            self.assertEqual(authenticated.status, 302)
            self.assertIn("cea_admin_session=", authenticated.headers["Set-Cookie"])

            dashboard = await client.get("/")
            self.assertEqual(dashboard.status, 200)
            self.assertIn("frame-ancestors 'none'", dashboard.headers["Content-Security-Policy"])
            self.assertIn("remote-admin-token", await dashboard.text())

            logout = await client.post("/logout", allow_redirects=False)
            self.assertEqual(logout.status, 302)
            after_logout = await client.get("/", allow_redirects=False)
            self.assertEqual(after_logout.status, 200)
            self.assertIn("Вход в админку", await after_logout.text())
        finally:
            await client.close()

    def test_remote_session_tokens_expire_and_reject_tampering(self) -> None:
        secret = "x" * 48
        token = _session_token(secret, now=1_000)
        self.assertTrue(_valid_session(token, secret, now=1_001))
        self.assertFalse(
            _valid_session(token, secret, now=1_000 + 7 * 24 * 60 * 60 + 1)
        )
        self.assertFalse(_valid_session(token + "tampered", secret, now=1_001))

    def test_remote_dashboard_rejects_short_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "ADMIN_WEB_PASSWORD"):
            create_admin_app(
                db=self.db,
                settings=replace(
                    self.settings,
                    admin_web_password="short",
                    admin_web_session_secret="s" * 48,
                ),
                require_login=True,
            )
