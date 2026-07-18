from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.vpn_worker_nonces import VpnWorkerNonceRepository
from ceai.services.app import AppServices
from ceai.services.exceptions import BusinessRuleError
from ceai.services.vpn import VpnJobCompletion
from ceai.time_utils import iso_now


WORKER_ID_HEADER = "X-CEA-VPN-Worker-ID"
TIMESTAMP_HEADER = "X-CEA-VPN-Timestamp"
NONCE_HEADER = "X-CEA-VPN-Nonce"
SIGNATURE_HEADER = "X-CEA-VPN-Signature"
MAX_BODY_BYTES = 32 * 1024


def canonical_worker_request(
    *,
    method: str,
    path_query: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return (
        f"{method.upper()}\n{path_query}\n{timestamp}\n{nonce}\n{body_hash}"
    ).encode("utf-8")


class VpnWorkerAuthenticator:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.nonces = VpnWorkerNonceRepository()

    def authorize(
        self,
        *,
        method: str,
        path_query: str,
        headers: Any,
        body: bytes,
    ) -> str:
        secret = self.settings.vpn_worker_secret
        if len(secret) < 32:
            raise web.HTTPServiceUnavailable(text="VPN worker is not configured")

        worker_id = str(headers.get(WORKER_ID_HEADER, "")).strip()
        timestamp = str(headers.get(TIMESTAMP_HEADER, "")).strip()
        nonce = str(headers.get(NONCE_HEADER, "")).strip()
        signature = str(headers.get(SIGNATURE_HEADER, "")).strip().lower()
        if (
            worker_id != self.settings.vpn_worker_id
            or not timestamp
            or not (16 <= len(nonce) <= 128)
            or len(signature) != 64
        ):
            raise web.HTTPUnauthorized(text="Invalid VPN worker authentication")

        try:
            timestamp_number = int(timestamp)
        except ValueError as exc:
            raise web.HTTPUnauthorized(
                text="Invalid VPN worker authentication"
            ) from exc
        if abs(int(time.time()) - timestamp_number) > max(
            30, self.settings.vpn_worker_clock_skew_seconds
        ):
            raise web.HTTPUnauthorized(text="Expired VPN worker request")

        canonical = canonical_worker_request(
            method=method,
            path_query=path_query,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        expected = hmac.new(
            secret.encode("utf-8"), canonical, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise web.HTTPUnauthorized(text="Invalid VPN worker authentication")

        with self.db.transaction() as conn:
            consumed = self.nonces.consume(
                conn,
                worker_id=worker_id,
                nonce=nonce,
                seen_at=iso_now(),
                ttl_seconds=max(
                    120,
                    self.settings.vpn_worker_clock_skew_seconds * 2,
                ),
            )
        if not consumed:
            raise web.HTTPConflict(text="VPN worker request was already used")
        return worker_id


CompletionCallback = Callable[[VpnJobCompletion], Awaitable[None]]


def register_vpn_worker_routes(
    app: web.Application,
    *,
    db: Database,
    services: AppServices,
    settings: Settings,
    on_completed: CompletionCallback | None = None,
) -> None:
    authenticator = VpnWorkerAuthenticator(db, settings)

    async def read_request(request: web.Request) -> tuple[str, Dict[str, Any]]:
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY_BYTES,
                actual_size=len(body),
            )
        worker_id = authenticator.authorize(
            method=request.method,
            path_query=request.path_qs,
            headers=request.headers,
            body=body,
        )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Invalid JSON object")
        payload_worker_id = str(payload.get("worker_id") or worker_id)
        if payload_worker_id != worker_id:
            raise web.HTTPForbidden(text="Worker identity mismatch")
        return worker_id, payload

    async def claim(request: web.Request) -> web.Response:
        worker_id, payload = await read_request(request)
        try:
            job = services.vpn.claim_worker_job(
                worker_id=worker_id,
                lease_seconds=max(30, min(settings.vpn_worker_lease_seconds, 600)),
                control_plane_ready=payload.get("control_plane_ready") is True,
                worker_inbound_tags=payload.get("inbound_tags"),
            )
        except BusinessRuleError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        return web.json_response({"ok": True, "job": job})

    async def result(request: web.Request) -> web.Response:
        worker_id, payload = await read_request(request)
        try:
            job_id = int(payload["job_id"])
            lease_token = str(payload["lease_token"])
            subscription_url = str(payload.get("subscription_url") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="Invalid VPN job result") from exc
        try:
            completion = services.vpn.complete_worker_job(
                worker_id=worker_id,
                job_id=job_id,
                lease_token=lease_token,
                subscription_url=subscription_url,
            )
        except BusinessRuleError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        if on_completed is not None:
            await on_completed(completion)
        return web.json_response({"ok": True})

    async def fail(request: web.Request) -> web.Response:
        worker_id, payload = await read_request(request)
        try:
            job_id = int(payload["job_id"])
            lease_token = str(payload["lease_token"])
            error_message = str(payload.get("error") or "worker failure")
        except (KeyError, TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="Invalid VPN job failure") from exc
        try:
            services.vpn.fail_worker_job(
                worker_id=worker_id,
                job_id=job_id,
                lease_token=lease_token,
                error_message=error_message,
            )
        except BusinessRuleError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok": True})

    app.router.add_post("/internal/vpn/worker/claim", claim)
    app.router.add_post("/internal/vpn/worker/result", result)
    app.router.add_post("/internal/vpn/worker/fail", fail)


__all__ = [
    "NONCE_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WORKER_ID_HEADER",
    "VpnWorkerAuthenticator",
    "canonical_worker_request",
    "register_vpn_worker_routes",
]
