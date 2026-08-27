from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from ceavpn.database import Database
from ceavpn.repositories.vpn_plans import VpnPlanRepository
from ceavpn.repositories.vpn_servers import VpnServerRepository
from ceavpn.repositories.vpn_subscription_devices import (
    DeviceLimitExceededError,
    VpnSubscriptionDeviceRepository,
)
from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceavpn.services.users import UserService
from ceavpn.time_utils import utcnow


class VpnSubscriptionDeviceRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        self.devices = VpnSubscriptionDeviceRepository()
        user = UserService(self.db).ensure_telegram_user(
            telegram_id=92201,
            username="devices_user",
            first_name="Devices",
            last_name="User",
            language_code="ru",
        )
        with self.db.transaction() as conn:
            server = VpnServerRepository().upsert(
                conn,
                code="nl-1",
                name="Нидерланды",
                provider="marzban",
                region="NL",
                api_base_url="http://127.0.0.1:8000",
                worker_id="worker-nl",
                subscription_base_url="https://sub.example.test",
            )
            plan = VpnPlanRepository().upsert(
                conn,
                code="vpn-1m",
                name="1 месяц",
                duration_days=30,
                price_rub=179,
                price_stars=139,
                max_devices=2,
            )
            now = utcnow()
            self.subscription = VpnSubscriptionRepository().create_provisioning(
                conn,
                user_id=int(user["id"]),
                server_id=int(server["id"]),
                plan_id=int(plan["id"]),
                kind="paid",
                provider_username="u_devices",
                starts_at=now.isoformat(),
                ends_at=(now + timedelta(days=30)).isoformat(),
            )

    def tearDown(self) -> None:
        self.db.close()

    def _register(
        self,
        key: str,
        *,
        limit: int = 2,
        model: str = "iPhone",
        user_agent: str = "Happ/5.6.0/iOS",
        legacy_user_agent_suffix: str = "",
    ):
        with self.db.transaction() as conn:
            return self.devices.register_or_touch(
                conn,
                subscription_id=int(self.subscription["id"]),
                device_key=key,
                model=model,
                platform="iOS / 18",
                user_agent=user_agent,
                max_devices=limit,
                legacy_user_agent_suffix=legacy_user_agent_suffix,
            )

    def test_base_limit_allows_two_and_blocks_third(self) -> None:
        self._register("device-one")
        self._register("device-two")
        self._register("device-one")
        with self.db.transaction() as conn:
            self.assertEqual(
                self.devices.active_count(
                    conn, subscription_id=int(self.subscription["id"])
                ),
                2,
            )
        with self.assertRaises(DeviceLimitExceededError):
            self._register("device-three")

    def test_purchased_slots_allow_more_devices(self) -> None:
        self._register("device-one", limit=4)
        self._register("device-two", limit=4)
        self._register("device-three", limit=4)
        self._register("device-four", limit=4)
        with self.assertRaises(DeviceLimitExceededError):
            self._register("device-five", limit=4)

    def test_detach_frees_slot_and_reconnect_reactivates_device(self) -> None:
        first = self._register("device-one")
        self._register("device-two")
        with self.db.transaction() as conn:
            self.assertTrue(
                self.devices.deactivate(
                    conn,
                    subscription_id=int(self.subscription["id"]),
                    device_id=int(first["id"]),
                )
            )
        self._register("device-three")
        with self.assertRaises(DeviceLimitExceededError):
            self._register("device-one")

    def test_happ_identity_merges_legacy_ip_duplicates_at_the_limit(self) -> None:
        user_agent = "Happ/5.6.0/ios/2608171408651"
        self._register(
            "legacy-ip-one",
            model="Не определено",
            user_agent=user_agent,
        )
        self._register(
            "legacy-ip-two",
            model="iPhone 13 Pro",
            user_agent=user_agent,
        )

        refreshed = self._register(
            "stable-happ-key",
            model="iPhone",
            user_agent=user_agent,
            legacy_user_agent_suffix="/ios/2608171408651",
        )

        with self.db.transaction() as conn:
            self.assertEqual(
                self.devices.active_count(
                    conn, subscription_id=int(self.subscription["id"])
                ),
                1,
            )
            active = self.devices.list_active(
                conn,
                subscription_id=int(self.subscription["id"]),
                offset=0,
                limit=10,
            )
        self.assertEqual(refreshed["device_key"], "stable-happ-key")
        self.assertEqual(refreshed["model"], "iPhone 13 Pro")
        self.assertEqual(active[0]["model"], "iPhone 13 Pro")

    def test_parallel_registrations_never_exceed_limit(self) -> None:
        barrier = threading.Barrier(3)
        results: list[bool] = []

        def worker(key: str) -> None:
            barrier.wait()
            try:
                self._register(key)
                results.append(True)
            except DeviceLimitExceededError:
                results.append(False)

        threads = [threading.Thread(target=worker, args=(f"device-{index}",)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(True), 2)
        self.assertEqual(results.count(False), 1)


if __name__ == "__main__":
    unittest.main()
