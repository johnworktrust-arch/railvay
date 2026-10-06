from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ceavpn.database import Database
from ceavpn.services.users import UserService
from ceavpn.services.vpn_admin import VpnAdminService

# Exercise the independent web-admin implementation against the same schema.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "CeaAdmin"))
from ceaadmin.services.vpn_admin import VpnAdminService as WebVpnAdminService


@pytest.fixture(params=[VpnAdminService, WebVpnAdminService])
def vip(request):
    db = Database("sqlite:///:memory:")
    db.migrate()
    user = UserService(db).ensure_telegram_user(
        telegram_id=12345, username="vip_test", first_name="VIP",
        last_name=None, language_code="ru",
    )
    service = request.param(db, SimpleNamespace(vpn_worker_health_max_age_seconds=120))
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        for code, age in [("de-1", 86400), ("nl-1", 0)]:
            server = service.servers.upsert(
                conn, code=code, name=code, provider="marzban", region=code[:2],
                api_base_url="https://example.test", worker_id=code,
            )
            service.servers.mark_healthy(
                conn, server_id=server["id"], checked_at=(now-timedelta(seconds=age)).isoformat(),
            )
    yield service, user["id"], now
    db.close()


def create(vip, *, expired=False, url=True, username="original_user", server_id=2):
    service, user_id, now = vip
    with service.db.transaction() as conn:
        sub = service.subscriptions.create_provisioning(
            conn, user_id=user_id, server_id=server_id, plan_id=None, kind="trial",
            provider_username=username, starts_at=(now-timedelta(days=4)).isoformat(),
            ends_at=(now+timedelta(days=-1 if expired else 2)).isoformat(),
        )
        if url:
            sub = service.subscriptions.mark_active(
                conn, subscription_id=sub["id"], subscription_url="https://example.test/sub/old",
            )
        if expired:
            service.subscriptions.mark_status(conn, subscription_id=sub["id"], status="disabled")
        return sub


@pytest.mark.parametrize("expired", [False, True])
def test_vip_extends_existing_identity_and_url(vip, expired):
    service, user_id, now = vip
    old = create(vip, expired=expired)
    service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        rows = service.subscriptions.list_for_user(conn, user_id)
        assert len(rows) == 1
        assert rows[0]["id"] == old["id"]
        assert rows[0]["subscription_url"] == old["subscription_url"]
        assert rows[0]["provider_username"] == old["provider_username"]
        assert datetime.fromisoformat(rows[0]["ends_at"]) > now+timedelta(days=3649)
        assert rows[0]["kind"] == "paid"
        jobs = conn.execute("SELECT * FROM vpn_provisioning_jobs").fetchall()
        assert len(jobs) == 1
        assert jobs[0]["server_id"] == 2
        assert jobs[0]["operation"] == "update"


def test_vip_recovers_old_url_over_stuck_duplicate(vip):
    service, user_id, now = vip
    old = create(vip, expired=True)
    duplicate = create(vip, url=False, username="vip_broken", server_id=1)
    with service.db.transaction() as conn:
        service.jobs.enqueue(conn, subscription_id=duplicate["id"], server_id=1,
                             operation="create", idempotency_key="broken")
    service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        assert service.subscriptions.get_by_id(conn, old["id"])["status"] == "provisioning"
        assert service.subscriptions.get_by_id(conn, duplicate["id"])["status"] == "disabled"
        job = conn.execute("SELECT * FROM vpn_provisioning_jobs WHERE idempotency_key='broken'").fetchone()
        assert job["status"] == "completed"


def test_new_vip_uses_fresh_worker_not_stale_germany(vip):
    service, user_id, _ = vip
    service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        rows = service.subscriptions.list_for_user(conn, user_id)
        assert len(rows) == 1
        assert rows[0]["server_id"] == 2


def test_no_healthy_worker_rolls_back_vip(vip):
    service, user_id, _ = vip
    with service.db.transaction() as conn:
        conn.execute("UPDATE vpn_servers SET last_health_at=NULL")
    with pytest.raises(Exception, match="Нет доступного VPN-сервера"):
        service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        assert conn.execute("SELECT * FROM vpn_vip_users").fetchone() is None
        assert service.subscriptions.list_for_user(conn, user_id) == []


def test_pending_expiry_is_superseded_and_repeat_grant_keeps_identity(vip):
    service, user_id, _ = vip
    old = create(vip, expired=True)
    with service.db.transaction() as conn:
        service.jobs.enqueue(conn, subscription_id=old["id"], server_id=2,
                             operation="disable", idempotency_key="expiry")
    service.grant_vip(user_id=user_id)
    service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        rows = service.subscriptions.list_for_user(conn, user_id)
        assert len(rows) == 1
        assert rows[0]["id"] == old["id"]
        assert conn.execute("SELECT status FROM vpn_provisioning_jobs WHERE idempotency_key='expiry'").fetchone()["status"] == "completed"


def test_pending_paid_access_is_not_replaced_by_old_trial(vip):
    service, user_id, _ = vip
    create(vip, expired=True)
    pending = create(vip, url=False, username="paid_order")
    service.grant_vip(user_id=user_id)
    with service.db.transaction() as conn:
        assert service.subscriptions.get_live_for_user(conn, user_id)["id"] == pending["id"]
