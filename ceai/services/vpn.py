from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.parse import urlsplit

from ceai.database import Database
from ceai.repositories.vpn_provisioning_jobs import VpnProvisioningJobRepository
from ceai.repositories.vpn_servers import VpnServerRepository
from ceai.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceai.repositories.vpn_trial_claims import VpnTrialClaimRepository
from ceai.services.exceptions import BusinessRuleError
from ceai.time_utils import iso_now, parse_iso, utcnow


@dataclass(frozen=True)
class VpnTrialOutcome:
    subscription: Dict[str, Any]
    created: bool
    trial_already_used: bool = False


@dataclass(frozen=True)
class VpnJobCompletion:
    subscription: Dict[str, Any]
    telegram_id: int
    operation: str


class VpnService:
    def __init__(
        self,
        db: Database,
        *,
        server_code: str,
        trial_days: int = 3,
    ) -> None:
        self.db = db
        self.server_code = server_code
        self.trial_days = trial_days
        self.servers = VpnServerRepository()
        self.subscriptions = VpnSubscriptionRepository()
        self.trials = VpnTrialClaimRepository()
        self.jobs = VpnProvisioningJobRepository()

    def claim_trial(
        self,
        *,
        user_id: int,
        channel: str,
    ) -> VpnTrialOutcome:
        if self.trial_days <= 0:
            raise BusinessRuleError("Бесплатный период временно недоступен.")

        with self.db.transaction() as conn:
            existing_claim = self.trials.get_by_user_id(conn, user_id)
            if existing_claim is not None:
                subscription = self.subscriptions.get_by_id(
                    conn, int(existing_claim["subscription_id"])
                )
                if subscription is None:
                    raise RuntimeError("VPN trial points to a missing subscription")
                return VpnTrialOutcome(
                    subscription=subscription,
                    created=False,
                    trial_already_used=True,
                )

            live = self.subscriptions.get_live_for_user(conn, user_id)
            if live is not None:
                subscription = self.subscriptions.get_by_id(conn, int(live["id"]))
                if subscription is None:
                    raise RuntimeError("VPN subscription disappeared")
                return VpnTrialOutcome(subscription=subscription, created=False)

            server = self.servers.get_by_code(conn, self.server_code)
            if server is None or not bool(server["is_active"]):
                raise BusinessRuleError(
                    "Сервер сейчас готовится. Попробуйте ещё раз чуть позже."
                )

            starts_at = utcnow()
            subscription = self.subscriptions.create_provisioning(
                conn,
                user_id=user_id,
                server_id=int(server["id"]),
                plan_id=None,
                kind="trial",
                provider_username=f"u_{secrets.token_hex(12)}",
                starts_at=starts_at.isoformat(),
                ends_at=(starts_at + timedelta(days=self.trial_days)).isoformat(),
            )
            self.trials.create(
                conn,
                user_id=user_id,
                subscription_id=int(subscription["id"]),
                channel=channel,
            )
            self.jobs.enqueue(
                conn,
                subscription_id=int(subscription["id"]),
                operation="create",
                idempotency_key=f"vpn:create:{subscription['id']}",
            )
            return VpnTrialOutcome(subscription=subscription, created=True)

    def get_current_subscription(self, user_id: int) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            return self.subscriptions.get_latest_for_user(conn, user_id)

    def enqueue_due_expirations(self, *, limit: int = 100) -> int:
        queued = 0
        with self.db.transaction() as conn:
            due = self.subscriptions.list_due_for_expiration(conn, limit=limit)
            for subscription in due:
                subscription_id = int(subscription["id"])
                if subscription["status"] == "active":
                    self.subscriptions.mark_status(
                        conn,
                        subscription_id=subscription_id,
                        status="expired",
                    )
                _, created = self.jobs.enqueue(
                    conn,
                    subscription_id=subscription_id,
                    operation="disable",
                    idempotency_key=(
                        f"vpn:disable:{subscription_id}:"
                        f"{self._iso_value(subscription['ends_at'])}"
                    ),
                )
                queued += int(created)
        return queued

    def claim_worker_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> Dict[str, Any] | None:
        with self.db.transaction() as conn:
            server = self.servers.get_by_worker_id(conn, worker_id)
            if server is None or not bool(server["is_active"]):
                raise BusinessRuleError("Unknown or inactive VPN worker")

            due = self.subscriptions.list_due_for_expiration(conn, limit=100)
            for subscription in due:
                if int(subscription["server_id"]) != int(server["id"]):
                    continue
                subscription_id = int(subscription["id"])
                if subscription["status"] == "active":
                    self.subscriptions.mark_status(
                        conn,
                        subscription_id=subscription_id,
                        status="expired",
                    )
                self.jobs.enqueue(
                    conn,
                    subscription_id=subscription_id,
                    operation="disable",
                    idempotency_key=(
                        f"vpn:disable:{subscription_id}:"
                        f"{self._iso_value(subscription['ends_at'])}"
                    ),
                )

            job = self.jobs.claim_due(
                conn,
                lease_seconds=lease_seconds,
                server_id=int(server["id"]),
            )
            if job is None:
                self.servers.mark_healthy(conn, server_id=int(server["id"]))
                return None

            subscription = self.subscriptions.get_by_id(
                conn, int(job["subscription_id"])
            )
            if subscription is None:
                raise RuntimeError("VPN job points to a missing subscription")

            operation = str(job["operation"])
            desired_status = "disabled" if operation == "disable" else "active"
            payload: Dict[str, Any] = {
                "username": subscription["provider_username"],
                "status": desired_status,
            }
            if operation != "disable":
                payload.update(
                    {
                        "proxies": {
                            "vless": {"flow": "xtls-rprx-vision"},
                        },
                        "inbounds": {"vless": ["VLESS TCP REALITY"]},
                        "expire": int(self._datetime(subscription["ends_at"]).timestamp()),
                        "data_limit": 0,
                        "data_limit_reset_strategy": "no_reset",
                        "note": f"CEA VPN subscription {subscription['id']}",
                    }
                )

            return {
                "job_id": int(job["id"]),
                "lease_token": job["lease_token"],
                "operation": operation,
                "attempt": int(job["attempts"]),
                "marzban_user": payload,
                "subscription_base_url": server["subscription_base_url"],
            }

    def complete_worker_job(
        self,
        *,
        worker_id: str,
        job_id: int,
        lease_token: str,
        subscription_url: str = "",
    ) -> VpnJobCompletion:
        with self.db.transaction() as conn:
            server = self._require_worker_server(conn, worker_id)
            job = self._require_worker_job(
                conn,
                server_id=int(server["id"]),
                job_id=job_id,
                lease_token=lease_token,
            )
            subscription_id = int(job["subscription_id"])
            operation = str(job["operation"])

            if operation == "disable":
                subscription = self.subscriptions.mark_status(
                    conn,
                    subscription_id=subscription_id,
                    status="disabled",
                )
            else:
                self._validate_subscription_url(
                    subscription_url,
                    str(server["subscription_base_url"]),
                )
                subscription = self.subscriptions.mark_active(
                    conn,
                    subscription_id=subscription_id,
                    subscription_url=subscription_url,
                )
                if subscription["kind"] == "trial":
                    claim = self.trials.get_by_subscription_id(conn, subscription_id)
                    if claim is not None:
                        self.trials.mark_status(
                            conn,
                            claim_id=int(claim["id"]),
                            status="provisioned",
                        )

            self.jobs.mark_completed(
                conn,
                job_id=job_id,
                lease_token=lease_token,
            )
            self.servers.mark_healthy(conn, server_id=int(server["id"]))
            user = conn.execute(
                "SELECT telegram_id FROM users WHERE id = ?",
                (int(subscription["user_id"]),),
            ).fetchone()
            if user is None:
                raise RuntimeError("VPN subscription user is missing")
            return VpnJobCompletion(
                subscription=subscription,
                telegram_id=int(user["telegram_id"]),
                operation=operation,
            )

    def fail_worker_job(
        self,
        *,
        worker_id: str,
        job_id: int,
        lease_token: str,
        error_message: str,
    ) -> None:
        with self.db.transaction() as conn:
            server = self._require_worker_server(conn, worker_id)
            job = self._require_worker_job(
                conn,
                server_id=int(server["id"]),
                job_id=job_id,
                lease_token=lease_token,
            )
            attempts = max(1, int(job["attempts"]))
            delay_seconds = min(900, 5 * (2 ** min(attempts - 1, 8)))
            next_attempt = utcnow() + timedelta(seconds=delay_seconds)
            clean_error = " ".join(error_message.split())[:500] or "worker failure"
            self.jobs.mark_failed(
                conn,
                job_id=job_id,
                lease_token=lease_token,
                error_message=clean_error,
                next_attempt_at=next_attempt.isoformat(),
            )
            if attempts >= 5 and str(job["operation"]) != "disable":
                self.subscriptions.mark_status(
                    conn,
                    subscription_id=int(job["subscription_id"]),
                    status="error",
                    last_error="Provisioning is being retried",
                )

    def _require_worker_server(self, conn: Any, worker_id: str) -> Dict[str, Any]:
        server = self.servers.get_by_worker_id(conn, worker_id)
        if server is None:
            raise BusinessRuleError("Unknown VPN worker")
        return server

    def _require_worker_job(
        self,
        conn: Any,
        *,
        server_id: int,
        job_id: int,
        lease_token: str,
    ) -> Dict[str, Any]:
        job = self.jobs.get_by_id(conn, job_id)
        if (
            job is None
            or job["status"] != "running"
            or not secrets.compare_digest(str(job.get("lease_token") or ""), lease_token)
        ):
            raise BusinessRuleError("VPN worker lease is no longer valid")
        subscription = self.subscriptions.get_by_id(
            conn, int(job["subscription_id"])
        )
        if subscription is None or int(subscription["server_id"]) != server_id:
            raise BusinessRuleError("VPN job belongs to another worker")
        return job

    @staticmethod
    def _validate_subscription_url(value: str, base_url: str) -> None:
        base = base_url.strip().rstrip("/")
        candidate = value.strip()
        base_parts = urlsplit(base)
        candidate_parts = urlsplit(candidate)
        if (
            base_parts.scheme != "https"
            or not base_parts.netloc
            or candidate_parts.scheme != "https"
            or candidate_parts.netloc != base_parts.netloc
            or not candidate.startswith(f"{base}/sub/")
        ):
            raise BusinessRuleError("Worker returned an invalid subscription URL")

    @staticmethod
    def _datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_iso(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _iso_value(cls, value: Any) -> str:
        return cls._datetime(value).isoformat()


__all__ = ["VpnJobCompletion", "VpnService", "VpnTrialOutcome"]
