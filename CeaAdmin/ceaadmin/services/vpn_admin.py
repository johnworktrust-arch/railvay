from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from ceaadmin.config import Settings
from ceaadmin.database import Database
from ceaadmin.repositories.vpn_abuse import VpnAbuseRepository
from ceaadmin.repositories.vpn_admin import VpnAdminRepository
from ceaadmin.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository
from ceaadmin.repositories.vpn_servers import VpnServerRepository
from ceaadmin.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceaadmin.services.exceptions import NotFoundError


class VpnAdminService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.repository = VpnAdminRepository()
        self.abuse = VpnAbuseRepository()
        self.subscriptions = VpnSubscriptionRepository()
        self.jobs = VpnProvisioningJobRepository()
        self.servers = VpnServerRepository()

    def _times(self) -> tuple[str, str, str]:
        now = datetime.now(timezone.utc)
        period_started_at = datetime.now(ZoneInfo("Europe/Moscow")).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(timezone.utc)
        health_window = max(
            30,
            int(self.settings.vpn_worker_health_max_age_seconds),
        )
        return (
            now.isoformat(),
            period_started_at.isoformat(),
            (now - timedelta(seconds=health_window)).isoformat(),
        )

    def dashboard(self) -> Dict[str, Any]:
        now, period_started_at, healthy_after = self._times()
        with self.db.transaction() as conn:
            stats = self.repository.dashboard_stats(
                conn,
                now=now,
                period_started_at=period_started_at,
                healthy_after=healthy_after,
            )
            stats["servers"] = self.repository.list_servers(
                conn,
                now=now,
                healthy_after=healthy_after,
            )
        total = int(stats.get("users_total") or 0)
        paid = int(stats.get("paid_users") or 0)
        stats["conversion_percent"] = round(paid * 100 / total, 1) if total else 0
        return stats

    def list_users(
        self,
        *,
        page: int,
        page_size: int = 25,
        query: str = "",
        segment: str = "all",
    ) -> Dict[str, Any]:
        page_size = min(max(page_size, 1), 100)
        allowed_segments = {
            "all",
            "trial",
            "paid",
            "active",
            "expired",
            "issues",
            "blocked",
        }
        segment = segment if segment in allowed_segments else "all"
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            total = self.repository.count_users(
                conn,
                query=query,
                segment=segment,
                now=now,
            )
            pages = max(math.ceil(total / page_size), 1)
            page = min(max(page, 1), pages)
            users = self.repository.list_users(
                conn,
                page=page,
                page_size=page_size,
                query=query,
                segment=segment,
                now=now,
            )
        return {
            "users": users,
            "page": page,
            "pages": pages,
            "total": total,
            "segment": segment,
            "query": query,
        }

    def user_card(self, user_id: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            card = self.repository.user_card(
                conn,
                user_id=user_id,
                now=now,
            )
        if card is None:
            raise NotFoundError("VPN-пользователь не найден")
        return card

    def set_abuse_blocked(
        self,
        *,
        user_id: int,
        is_blocked: bool,
        reason: str = "",
        admin_user_id: int | None = None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            exists = conn.execute(
                """
                SELECT 1 AS exists
                FROM users u
                WHERE u.id = ?
                  AND (
                    EXISTS (
                        SELECT 1 FROM vpn_subscriptions s
                        WHERE s.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_payments pay
                        WHERE pay.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_trial_claims claim
                        WHERE claim.user_id = u.id
                    )
                  )
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError("VPN-пользователь не найден")

            self.abuse.set_blocked(
                conn,
                user_id=user_id,
                is_blocked=is_blocked,
                reason=reason,
                admin_user_id=admin_user_id,
            )
            if is_blocked:
                subscriptions = self.subscriptions.list_for_user(conn, user_id)
                for subscription in subscriptions:
                    subscription_id = int(subscription["id"])
                    if str(subscription.get("status") or "") != "disabled":
                        subscription = self.subscriptions.mark_status(
                            conn,
                            subscription_id=subscription_id,
                            status="disabled",
                            last_error="VPN access blocked by admin",
                        )
                    self._enqueue_disable_for_active_servers(
                        conn,
                        subscription=subscription,
                        base_idempotency_key=(
                            f"vpn:abuse-ban:{subscription_id}:{now}"
                        ),
                    )

            card = self.repository.user_card(conn, user_id=user_id, now=now)
        if card is None:
            raise NotFoundError("VPN-пользователь не найден")
        return card

    def _enqueue_disable_for_active_servers(
        self,
        conn: Any,
        *,
        subscription: Dict[str, Any],
        base_idempotency_key: str,
    ) -> int:
        subscription_id = int(subscription["id"])
        canonical_server_id = int(subscription["server_id"])
        servers = self.servers.list_active(conn)
        if all(int(server["id"]) != canonical_server_id for server in servers):
            canonical = self.servers.get_by_id(conn, canonical_server_id)
            if canonical is not None:
                servers.append(canonical)

        created_count = 0
        for server in servers:
            server_id = int(server["id"])
            idempotency_key = (
                base_idempotency_key
                if server_id == canonical_server_id
                else f"{base_idempotency_key}:server:{server_id}"
            )
            _, created = self.jobs.enqueue(
                conn,
                subscription_id=subscription_id,
                server_id=server_id,
                operation="disable",
                idempotency_key=idempotency_key,
            )
            created_count += int(created)
        return created_count
