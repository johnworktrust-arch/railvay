from __future__ import annotations

import unittest
from unittest.mock import patch

from ceavpn.config import load_settings
from ceavpn.database import Database
from ceavpn.seed import seed_reference_data


VALID_SERVERS_JSON = (
    '[{"code":"ru-wl-1","name":"Whitelist staging",'
    '"region":"RU","worker_id":"cea-vpn-ru-wl-1",'
    '"subscription_base_url":"https://sub.example.test:8443",'
    '"is_active":true}]'
)
VALID_WORKER_SECRETS_JSON = (
    '{"cea-vpn-ru-wl-1":"'
    "0123456789abcdef0123456789abcdef"
    '"}'
)


class VpnAdditionalServerSettingsTest(unittest.TestCase):
    def load(self, payload: str):
        with (
            patch("ceavpn.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {"VPN_ADDITIONAL_SERVERS_JSON": payload},
                clear=True,
            ),
        ):
            return load_settings()

    def test_reads_strict_additional_server_contract(self) -> None:
        settings = self.load(VALID_SERVERS_JSON)

        self.assertEqual(len(settings.vpn_additional_servers), 1)
        server = settings.vpn_additional_servers[0]
        self.assertEqual(server.code, "ru-wl-1")
        self.assertEqual(server.worker_id, "cea-vpn-ru-wl-1")
        self.assertEqual(
            server.subscription_base_url,
            "https://sub.example.test:8443",
        )
        self.assertTrue(server.is_active)

    def test_rejects_unsafe_additional_server_contracts(self) -> None:
        invalid_payloads = (
            "{}",
            '[{"code":"ru-wl-1"}]',
            VALID_SERVERS_JSON.replace(":8443", ":443"),
            VALID_SERVERS_JSON.replace('"is_active":true', '"is_active":"true"'),
            VALID_SERVERS_JSON.replace(
                "https://sub.example.test:8443",
                "http://127.0.0.1:8443",
            ),
            VALID_SERVERS_JSON.replace(
                "cea-vpn-ru-wl-1",
                "cea.vpn.ru.wl.1",
            ),
            f"[{VALID_SERVERS_JSON[1:-1]},{VALID_SERVERS_JSON[1:-1]}]",
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.load(payload)

    def test_worker_secret_ids_use_the_worker_runtime_contract(self) -> None:
        invalid_payloads = (
            '{"cea.vpn.ru":"0123456789abcdef0123456789abcdef"}',
            '{"cea-vpn-ru":"0123456789abcdef0123456789abcdef",'
            '" cea-vpn-ru ":"abcdef0123456789abcdef0123456789"}',
            '{"cea-vpn-ru":"0123456789abcdef0123456789abcdef",'
            '"cea-vpn-ru":"abcdef0123456789abcdef0123456789"}',
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with (
                    patch("ceavpn.config._load_dotenv", return_value={}),
                    patch.dict(
                        "os.environ",
                        {"VPN_WORKER_SECRETS_JSON": payload},
                        clear=True,
                    ),
                ):
                    with self.assertRaises(ValueError):
                        load_settings()

    def test_seed_registers_staging_worker_without_making_it_canonical(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {
                        "VPN_ADDITIONAL_SERVERS_JSON": VALID_SERVERS_JSON,
                        "VPN_WORKER_SECRETS_JSON": VALID_WORKER_SECRETS_JSON,
                    },
                    clear=True,
                ),
            ):
                seed_reference_data(db)

            with db.transaction() as conn:
                staging = conn.execute(
                    "SELECT * FROM vpn_servers WHERE code = ?",
                    ("ru-wl-1",),
                ).fetchone()
                canonical = conn.execute(
                    "SELECT * FROM vpn_servers WHERE code = ?",
                    ("nl-1",),
                ).fetchone()

            self.assertIsNotNone(staging)
            self.assertIsNotNone(canonical)
            assert staging is not None
            assert canonical is not None
            self.assertTrue(staging["is_active"])
            self.assertEqual(staging["worker_id"], "cea-vpn-ru-wl-1")
            self.assertEqual(staging["api_base_url"], "http://127.0.0.1:8000")
            self.assertEqual(canonical["worker_id"], "cea-vpn-nl1")
        finally:
            db.close()

    def test_seed_refuses_canonical_identity_collision(self) -> None:
        collision = VALID_SERVERS_JSON.replace('"code":"ru-wl-1"', '"code":"nl-1"')
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {
                        "VPN_ADDITIONAL_SERVERS_JSON": collision,
                        "VPN_WORKER_SECRETS_JSON": VALID_WORKER_SECRETS_JSON,
                    },
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "collides"):
                    seed_reference_data(db)
        finally:
            db.close()

    def test_seed_refuses_active_worker_without_its_secret_mapping(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {"VPN_ADDITIONAL_SERVERS_JSON": VALID_SERVERS_JSON},
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "per-worker secret"):
                    seed_reference_data(db)
        finally:
            db.close()

    def test_seed_deactivates_explicitly_and_omission_preserves_state(self) -> None:
        db = Database("sqlite:///:memory:")
        inactive_json = VALID_SERVERS_JSON.replace(
            '"is_active":true',
            '"is_active":false',
        )
        try:
            db.migrate()
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {
                        "VPN_ADDITIONAL_SERVERS_JSON": VALID_SERVERS_JSON,
                        "VPN_WORKER_SECRETS_JSON": VALID_WORKER_SECRETS_JSON,
                    },
                    clear=True,
                ),
            ):
                seed_reference_data(db)
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {"VPN_ADDITIONAL_SERVERS_JSON": inactive_json},
                    clear=True,
                ),
            ):
                seed_reference_data(db)
            with (
                patch("ceavpn.config._load_dotenv", return_value={}),
                patch.dict(
                    "os.environ",
                    {"VPN_ADDITIONAL_SERVERS_JSON": "[]"},
                    clear=True,
                ),
            ):
                seed_reference_data(db)

            with db.transaction() as conn:
                staging = conn.execute(
                    "SELECT is_active FROM vpn_servers WHERE code = ?",
                    ("ru-wl-1",),
                ).fetchone()

            self.assertIsNotNone(staging)
            assert staging is not None
            self.assertFalse(staging["is_active"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
