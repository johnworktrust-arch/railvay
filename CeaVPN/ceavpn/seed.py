from __future__ import annotations

from ceavpn.config import load_settings
from ceavpn.database import Database
from ceavpn.repositories.vpn_plans import VpnPlanRepository
from ceavpn.repositories.vpn_servers import VpnServerRepository

VPN_PLANS = [
    ("vpn-1m", "1 месяц", 30, 179, 139),
    ("vpn-3m", "3 месяца", 90, 469, 389),
    ("vpn-6m", "6 месяцев", 180, 780, 639),
    ("vpn-12m", "1 год", 365, 1280, 989),
    ("vpn-10y", "10 лет", 3650, 9990, 7790),
]

VPN_ADDITIONAL_SERVERS = (
    {
        "code": "ru-lte-1",
        "name": "Россия · Yandex 1",
        "provider": "marzban",
        "region": "RU",
        "api_base_url": "http://127.0.0.1:8000",
        "is_active": False,
        "worker_id": "cea-vpn-lte-1",
        "subscription_base_url": "https://sub.111-88-158-45.sslip.io:8443",
    },
    {
        "code": "us-1",
        "name": "CEA VPN Charlotte 1",
        "provider": "marzban",
        "region": "US",
        "api_base_url": "http://127.0.0.1:8000",
        "worker_id": "cea-vpn-us",
        "subscription_base_url": "https://sub.77-110-119-28.sslip.io:8443",
    },
    {
        "code": "fi-1",
        "name": "CEA VPN Helsinki 1",
        "provider": "marzban",
        "region": "FI",
        "api_base_url": "http://127.0.0.1:8000",
        "worker_id": "cea-vpn-fi",
        "subscription_base_url": "https://sub.138-124-59-14.sslip.io:8443",
    },
)


def seed_reference_data(db: Database) -> None:
    settings = load_settings()
    per_worker_secrets = dict(settings.vpn_worker_secrets)
    vpn_plan_repo = VpnPlanRepository()
    vpn_server_repo = VpnServerRepository()
    with db.transaction() as conn:
        for code, name, duration_days, price_rub, price_stars in VPN_PLANS:
            vpn_plan_repo.upsert(
                conn,
                code=code,
                name=name,
                duration_days=duration_days,
                price_rub=price_rub,
                price_stars=price_stars,
                max_devices=2,
            )
        vpn_server_repo.upsert(
            conn,
            code=settings.vpn_server_code,
            name="CEA VPN Amsterdam 1",
            provider="marzban",
            region="NL",
            api_base_url="http://127.0.0.1:8000",
            worker_id=settings.vpn_worker_id,
            subscription_base_url=settings.vpn_subscription_base_url,
        )
        reserved_codes = {settings.vpn_server_code}
        reserved_workers = {settings.vpn_worker_id}
        for server in VPN_ADDITIONAL_SERVERS:
            if (
                server["code"] in reserved_codes
                or server["worker_id"] in reserved_workers
            ):
                raise ValueError(
                    "Built-in VPN server identity collides with the canonical server"
                )
            vpn_server_repo.upsert(conn, **server)
            reserved_codes.add(server["code"])
            reserved_workers.add(server["worker_id"])
        for server in settings.vpn_additional_servers:
            if (
                server.code in reserved_codes
                or server.worker_id in reserved_workers
            ):
                raise ValueError(
                    "VPN_ADDITIONAL_SERVERS_JSON collides with a configured server"
                )
            if server.is_active and server.worker_id not in per_worker_secrets:
                raise ValueError(
                    "Active additional VPN server requires a per-worker secret"
                )
            vpn_server_repo.upsert(
                conn,
                code=server.code,
                name=server.name,
                provider="marzban",
                region=server.region,
                api_base_url="http://127.0.0.1:8000",
                is_active=server.is_active,
                worker_id=server.worker_id,
                subscription_base_url=server.subscription_base_url,
            )
            reserved_codes.add(server.code)
            reserved_workers.add(server.worker_id)


def main() -> None:
    settings = load_settings()
    db = Database(settings.database_url)
    db.migrate()
    seed_reference_data(db)
    db.close()
    print("CeaVPN Database migrated and seeded.")


if __name__ == "__main__":
    main()
