import unittest

from ceavpn.config import load_settings
from ceavpn.database import Database
from ceavpn.seed import seed_reference_data
from ceavpn.services.app import build_services


class TestVpnDualDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.ai_db = Database("sqlite:///:memory:")
        self.ai_db.migrate()

        self.vpn_db = Database("sqlite:///:memory:")
        self.vpn_db.migrate()

        self.settings = load_settings()

    def tearDown(self) -> None:
        self.ai_db.close()
        self.vpn_db.close()

    def test_dual_database_user_sync_and_isolation(self) -> None:
        services = build_services(
            self.ai_db, self.settings, vpn_db=self.vpn_db
        )

        # 1. Register user
        user = services.users.ensure_telegram_user(
            telegram_id=123456789,
            username="testuser",
            first_name="Test",
        )

        # 2. Check user exists in both DBs with exact same ID
        ai_user = self.ai_db.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (123456789,)
        ).fetchone()
        vpn_user = self.vpn_db.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (123456789,)
        ).fetchone()

        self.assertIsNotNone(ai_user)
        self.assertIsNotNone(vpn_user)
        self.assertEqual(dict(ai_user)["id"], dict(vpn_user)["id"])
        self.assertEqual(user["id"], dict(ai_user)["id"])

        # 3. Seed reference data and mark server healthy and ready
        seed_reference_data(self.vpn_db)
        from ceavpn.time_utils import iso_now
        with self.vpn_db.transaction() as conn:
            conn.execute(
                "UPDATE vpn_servers SET subscription_base_url = 'https://sub.example.net:8443', last_health_at = ?",
                (iso_now(),),
            )

        # 4. Claim trial via VpnService (should target vpn_db)
        outcome = services.vpn.claim_trial(
            user_id=int(user["id"]),
            channel="@ceafamily",
        )
        self.assertTrue(outcome.created)

        # 5. Verify vpn_subscriptions exists in vpn_db, but NOT in ai_db
        vpn_sub = self.vpn_db.conn.execute(
            "SELECT * FROM vpn_subscriptions WHERE user_id = ?", (user["id"],)
        ).fetchone()
        ai_sub = self.ai_db.conn.execute(
            "SELECT * FROM vpn_subscriptions WHERE user_id = ?", (user["id"],)
        ).fetchone()

        self.assertIsNotNone(vpn_sub)
        self.assertIsNone(ai_sub)


if __name__ == "__main__":
    unittest.main()
