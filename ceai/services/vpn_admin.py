from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.vpn_admin import VpnAdminRepository
from ceai.services.exceptions import NotFoundError


class VpnAdminService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.repository = VpnAdminRepository()

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
