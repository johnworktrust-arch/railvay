#!/usr/bin/env python3
"""Outbound-only CEA VPN provisioning worker.

The worker polls a signed Railway endpoint, applies the requested state through
Marzban's loopback-only API, and signs the result back to Railway.  It never
opens a listening socket.  Keep this file on the VPN host, not in Railway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPMessage
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


LOGGER = logging.getLogger("ceavpn.worker")
JSON_LIMIT_BYTES = 1_048_576
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
OPERATIONS = {"create", "update", "disable", "sync"}


class WorkerError(Exception):
    """A failure whose message is safe to store and log."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class ConfigurationError(WorkerError):
    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=False)


class ApiError(WorkerError):
    def __init__(
        self,
        service: str,
        status: int,
        *,
        retryable: Optional[bool] = None,
    ) -> None:
        if retryable is None:
            retryable = status == 429 or status >= 500
        self.service = service
        self.status = status
        super().__init__(f"{service}_http_{status}", retryable=retryable)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


Transport = Callable[[Request, float], HttpResponse]


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent redirects from forwarding signed bodies or bearer tokens."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Mapping[str, str], newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


def stdlib_transport(request: Request, timeout: float) -> HttpResponse:
    """Perform an HTTP request without exposing response bodies in errors."""

    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            body = response.read(JSON_LIMIT_BYTES + 1)
            if len(body) > JSON_LIMIT_BYTES:
                raise WorkerError("http_response_too_large", retryable=False)
            return HttpResponse(
                status=int(response.status),
                body=body,
                headers=dict(response.headers.items()),
            )
    except HTTPError as exc:
        # Read and discard a bounded body so the connection can be reused.  It
        # can contain credentials or subscription URLs and must never be logged.
        body = exc.read(JSON_LIMIT_BYTES + 1)
        return HttpResponse(
            status=int(exc.code),
            body=body[:JSON_LIMIT_BYTES],
            headers=dict(exc.headers.items()) if isinstance(exc.headers, HTTPMessage) else {},
        )
    except (URLError, TimeoutError, OSError) as exc:
        raise WorkerError("http_transport_error") from exc


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise ConfigurationError(f"missing_{name.lower()}")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = str(env.get(name) or default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"invalid_{name.lower()}") from exc
    if value <= 0:
        raise ConfigurationError(f"invalid_{name.lower()}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"invalid_{name.lower()}") from exc
    if value <= 0:
        raise ConfigurationError(f"invalid_{name.lower()}")
    return value


def _validated_https_base(value: str, variable: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"invalid_{variable.lower()}") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError(f"invalid_{variable.lower()}")
    if port == 0 or parsed.query or parsed.fragment:
        raise ConfigurationError(f"invalid_{variable.lower()}")
    return normalized


def _validated_local_marzban_base(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("marzban_base_url_must_be_loopback_http") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port == 0
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("marzban_base_url_must_be_loopback_http")
    return normalized


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    worker_secret: str
    railway_base_url: str
    subscription_base_url: str
    marzban_base_url: str
    marzban_username: str
    marzban_password: str
    inbound_tag: str = "VLESS TCP REALITY"
    claim_path: str = "/internal/vpn/worker/claim"
    result_path: str = "/internal/vpn/worker/result"
    fail_path: str = "/internal/vpn/worker/fail"
    poll_interval_seconds: float = 3.0
    http_timeout_seconds: float = 15.0
    lease_seconds: int = 60

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WorkerConfig":
        source = os.environ if env is None else env
        worker_id = _required_env(source, "VPN_WORKER_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", worker_id):
            raise ConfigurationError("invalid_vpn_worker_id")
        secret = _required_env(source, "VPN_WORKER_SECRET")
        if (
            len(secret) < 32
            or len(secret.encode("utf-8")) < 32
            or secret.lower().startswith("replace-with-")
        ):
            raise ConfigurationError("vpn_worker_secret_too_short")

        claim_path = str(
            source.get("VPN_WORKER_CLAIM_PATH") or "/internal/vpn/worker/claim"
        ).strip()
        result_path = str(
            source.get("VPN_WORKER_RESULT_PATH") or "/internal/vpn/worker/result"
        ).strip()
        fail_path = str(
            source.get("VPN_WORKER_FAIL_PATH") or "/internal/vpn/worker/fail"
        ).strip()
        for path in (claim_path, result_path, fail_path):
            if not path.startswith("/") or path.startswith("//") or "#" in path:
                raise ConfigurationError("invalid_worker_api_path")

        return cls(
            worker_id=worker_id,
            worker_secret=secret,
            railway_base_url=_validated_https_base(
                _required_env(source, "VPN_RAILWAY_BASE_URL"),
                "VPN_RAILWAY_BASE_URL",
            ),
            subscription_base_url=_validated_https_base(
                _required_env(source, "VPN_SUBSCRIPTION_BASE_URL"),
                "VPN_SUBSCRIPTION_BASE_URL",
            ),
            marzban_base_url=_validated_local_marzban_base(
                str(source.get("MARZBAN_BASE_URL") or "http://127.0.0.1:8000")
            ),
            marzban_username=_required_env(source, "MARZBAN_BOT_USERNAME"),
            marzban_password=_validated_runtime_secret(
                _required_env(source, "MARZBAN_BOT_PASSWORD"),
                "MARZBAN_BOT_PASSWORD",
            ),
            inbound_tag=str(source.get("MARZBAN_INBOUND_TAG") or "VLESS TCP REALITY").strip(),
            claim_path=claim_path,
            result_path=result_path,
            fail_path=fail_path,
            poll_interval_seconds=_positive_float(
                source, "VPN_WORKER_POLL_INTERVAL_SECONDS", 3.0
            ),
            http_timeout_seconds=_positive_float(
                source, "VPN_WORKER_HTTP_TIMEOUT_SECONDS", 15.0
            ),
            lease_seconds=_positive_int(source, "VPN_WORKER_LEASE_SECONDS", 60),
        )


def canonical_signature_input(
    method: str,
    path_query: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_digest = hashlib.sha256(body).hexdigest()
    return (
        f"{method.upper()}\n{path_query}\n{timestamp}\n{nonce}\n{body_digest}"
    ).encode("utf-8")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_json_object(body: bytes, code: str) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError(code, retryable=False) from exc
    if not isinstance(value, dict):
        raise WorkerError(code, retryable=False)
    return value


class SignedRailwayClient:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        transport: Transport = stdlib_transport,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
    ) -> None:
        self.config = config
        self.transport = transport
        self.clock = clock
        self.nonce_factory = nonce_factory

    def claim(self, *, control_plane_ready: bool) -> Optional[Dict[str, Any]]:
        if control_plane_ready is not True:
            raise WorkerError("marzban_not_ready", retryable=False)
        response = self._post(
            self.config.claim_path,
            {
                "worker_id": self.config.worker_id,
                "lease_seconds": self.config.lease_seconds,
                "control_plane_ready": True,
            },
        )
        if response.status == 204 or not response.body:
            return None
        if not 200 <= response.status < 300:
            raise ApiError("railway_claim", response.status)
        payload = _decode_json_object(response.body, "invalid_claim_response")
        job = payload.get("job", payload)
        if job is None:
            return None
        if not isinstance(job, dict):
            raise WorkerError("invalid_claim_job", retryable=False)
        return dict(job)

    def report_result(self, payload: Mapping[str, Any]) -> None:
        response = self._post(self.config.result_path, payload)
        if not 200 <= response.status < 300:
            raise ApiError("railway_result", response.status)

    def report_failure(self, payload: Mapping[str, Any]) -> None:
        response = self._post(self.config.fail_path, payload)
        if not 200 <= response.status < 300:
            raise ApiError("railway_fail", response.status)

    def _post(self, path: str, payload: Mapping[str, Any]) -> HttpResponse:
        body = _json_bytes(payload)
        url = urljoin(self.config.railway_base_url + "/", path.lstrip("/"))
        parsed = urlsplit(url)
        path_query = parsed.path or "/"
        if parsed.query:
            path_query += "?" + parsed.query
        timestamp = str(int(self.clock()))
        nonce = self.nonce_factory()
        canonical = canonical_signature_input(
            "POST", path_query, timestamp, nonce, body
        )
        signature = hmac.new(
            self.config.worker_secret.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CEA-VPN-Worker-ID": self.config.worker_id,
                "X-CEA-VPN-Timestamp": timestamp,
                "X-CEA-VPN-Nonce": nonce,
                "X-CEA-VPN-Signature": signature,
                "User-Agent": "cea-vpn-worker/1",
            },
        )
        return self.transport(request, self.config.http_timeout_seconds)


class LocalMarzbanClient:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        transport: Transport = stdlib_transport,
    ) -> None:
        self.config = config
        self.transport = transport
        self._token = ""

    def healthcheck(self) -> None:
        """Authenticate to Marzban and query a stable local API endpoint."""

        response = self._authorized_request(
            "GET", "/api/user/cea_worker_healthcheck"
        )
        # A missing sentinel user is the normal response.  Both statuses prove
        # that the loopback API accepted our bearer token and handled a query.
        if response.status in {200, 404}:
            return
        raise ApiError("marzban_healthcheck", response.status)

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        response = self._authorized_request(
            "GET", f"/api/user/{quote(_validated_username(username), safe='')}"
        )
        if response.status == 404:
            return None
        return self._require_object(response, "marzban_get_user")

    def create_user(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        response = self._authorized_request("POST", "/api/user", payload=payload)
        if response.status == 409:
            username = _validated_username(str(payload.get("username") or ""))
            existing = self.get_user(username)
            if existing is None:
                raise WorkerError("marzban_conflict_without_user")
            # Preserve credentials created by the first request if its response
            # was lost.  Only converge mutable lifecycle fields.
            return self.update_user(username, _mutable_user_fields(payload))
        return self._require_object(response, "marzban_create_user")

    def update_user(
        self, username: str, payload: Mapping[str, Any]
    ) -> Dict[str, Any]:
        response = self._authorized_request(
            "PUT",
            f"/api/user/{quote(_validated_username(username), safe='')}",
            payload=payload,
        )
        return self._require_object(response, "marzban_update_user")

    def ensure_active(
        self,
        *,
        username: str,
        expire: int,
        subscription_id: int,
        data_limit: int = 0,
    ) -> Dict[str, Any]:
        desired = {
            "username": _validated_username(username),
            "proxies": {
                "vless": {
                    "id": str(uuid.uuid4()),
                    "flow": "xtls-rprx-vision",
                }
            },
            "inbounds": {"vless": [self.config.inbound_tag]},
            "expire": _validated_expire(expire),
            "data_limit": _validated_data_limit(data_limit),
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": f"CEA VPN subscription {int(subscription_id)}",
        }
        existing = self.get_user(username)
        if existing is None:
            user = self.create_user(desired)
        else:
            user = self.update_user(username, _mutable_user_fields(desired))
        return self._ensure_subscription_url(user, username)

    def disable(self, username: str) -> Dict[str, Any]:
        existing = self.get_user(username)
        if existing is None:
            return {"status": "disabled"}
        return self.update_user(username, {"status": "disabled"})

    def _ensure_subscription_url(
        self, user: Mapping[str, Any], username: str
    ) -> Dict[str, Any]:
        result = dict(user)
        value = result.get("subscription_url")
        if not isinstance(value, str) or not value:
            refreshed = self.get_user(username)
            if refreshed is None:
                raise WorkerError("marzban_user_disappeared")
            result = refreshed
            value = result.get("subscription_url")
        if not isinstance(value, str) or not value:
            raise WorkerError("marzban_subscription_url_missing", retryable=False)
        value = _absolute_subscription_url(value, self.config.subscription_base_url)
        result["subscription_url"] = value
        return result

    def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> HttpResponse:
        token = self._token or self._authenticate()
        response = self._request(method, path, payload=payload, token=token)
        if response.status == 401:
            self._token = ""
            response = self._request(
                method,
                path,
                payload=payload,
                token=self._authenticate(),
            )
        return response

    def _authenticate(self) -> str:
        body = urlencode(
            {
                "username": self.config.marzban_username,
                "password": self.config.marzban_password,
                "grant_type": "password",
            }
        ).encode("utf-8")
        request = Request(
            self.config.marzban_base_url + "/api/admin/token",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = self.transport(request, self.config.http_timeout_seconds)
        if not 200 <= response.status < 300:
            raise ApiError("marzban_auth", response.status, retryable=response.status >= 500)
        payload = _decode_json_object(response.body, "invalid_marzban_token_response")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise WorkerError("invalid_marzban_token_response", retryable=False)
        self._token = token
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]],
        token: str,
    ) -> HttpResponse:
        body = _json_bytes(payload) if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.config.marzban_base_url + path,
            data=body,
            method=method,
            headers=headers,
        )
        return self.transport(request, self.config.http_timeout_seconds)

    @staticmethod
    def _require_object(response: HttpResponse, service: str) -> Dict[str, Any]:
        if not 200 <= response.status < 300:
            raise ApiError(service, response.status)
        return _decode_json_object(response.body, f"invalid_{service}_response")


def _validated_username(value: str) -> str:
    if not USERNAME_RE.fullmatch(value):
        raise WorkerError("invalid_provider_username", retryable=False)
    return value


def _validated_runtime_secret(value: str, variable: str) -> str:
    if value.lower().startswith("replace-with-"):
        raise ConfigurationError(f"placeholder_{variable.lower()}")
    return value


def _validated_expire(value: Any) -> int:
    try:
        expire = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkerError("invalid_expire", retryable=False) from exc
    if expire <= 0 or expire > 4_102_444_800:  # 2100-01-01 UTC
        raise WorkerError("invalid_expire", retryable=False)
    return expire


def _validated_data_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkerError("invalid_data_limit", retryable=False) from exc
    if limit < 0:
        raise WorkerError("invalid_data_limit", retryable=False)
    return limit


def _mutable_user_fields(payload: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {
        "expire",
        "data_limit",
        "data_limit_reset_strategy",
        "status",
        "note",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _validate_subscription_url(value: str, allowed_base: str) -> None:
    parsed = urlsplit(value)
    allowed_prefix = _subscription_prefix(allowed_base)
    allowed = urlsplit(allowed_prefix)
    try:
        parsed_port = parsed.port
        allowed_port = allowed.port
    except ValueError as exc:
        raise WorkerError("untrusted_subscription_url", retryable=False) from exc
    decoded_path_segments = unquote(parsed.path).split("/")
    if (
        any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
        or parsed.scheme != "https"
        or parsed.hostname != allowed.hostname
        or parsed_port != allowed_port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(segment in {".", ".."} for segment in decoded_path_segments)
        or not parsed.path.startswith(allowed.path)
        or len(parsed.path) <= len(allowed.path)
    ):
        raise WorkerError("untrusted_subscription_url", retryable=False)


def _absolute_subscription_url(value: str, allowed_base: str) -> str:
    candidate = value.strip()
    if candidate.startswith("/sub/"):
        candidate = _normalized_subscription_root(allowed_base) + candidate
    _validate_subscription_url(candidate, allowed_base)
    return candidate


def _subscription_prefix(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/")
    if path.endswith("/sub") or path == "/sub":
        return normalized + "/"
    return normalized + "/sub/"


def _normalized_subscription_root(base_url: str) -> str:
    prefix = _subscription_prefix(base_url).rstrip("/")
    if prefix.endswith("/sub"):
        prefix = prefix[:-4]
    return prefix.rstrip("/")


def _job_expire(job: Mapping[str, Any]) -> int:
    for key in ("expire", "expire_unix", "expires_at_unix"):
        if job.get(key) is not None:
            return _validated_expire(job[key])
    ends_at = job.get("ends_at") or job.get("expires_at")
    if not isinstance(ends_at, str) or not ends_at.strip():
        raise WorkerError("job_expire_missing", retryable=False)
    normalized = ends_at.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkerError("invalid_job_expire", retryable=False) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _validated_expire(int(parsed.timestamp()))


@dataclass(frozen=True)
class ProvisioningJob:
    job_id: int
    lease_token: str
    operation: str
    subscription_id: int
    provider_username: str
    expire: Optional[int]
    data_limit: int

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        subscription_base_url: Optional[str] = None,
    ) -> "ProvisioningJob":
        marzban_user = payload.get("marzban_user")
        if marzban_user is None:
            desired: Mapping[str, Any] = payload
        elif isinstance(marzban_user, Mapping):
            desired = marzban_user
        else:
            raise WorkerError("invalid_marzban_user", retryable=False)
        try:
            job_id = int(payload.get("job_id") or payload.get("id"))
        except (TypeError, ValueError) as exc:
            raise WorkerError("invalid_job_identity", retryable=False) from exc
        if job_id <= 0:
            raise WorkerError("invalid_job_identity", retryable=False)
        lease_token = str(payload.get("lease_token") or "")
        if len(lease_token) < 16 or len(lease_token) > 512:
            raise WorkerError("invalid_job_lease", retryable=False)
        operation = str(payload.get("operation") or "").lower()
        if operation not in OPERATIONS:
            raise WorkerError("invalid_job_operation", retryable=False)
        expected_status = "disabled" if operation == "disable" else "active"
        desired_status = str(desired.get("status") or expected_status).lower()
        if desired_status != expected_status:
            raise WorkerError("invalid_job_status", retryable=False)
        username = _validated_username(
            str(desired.get("username") or payload.get("provider_username") or "")
        )

        raw_subscription_id = payload.get("subscription_id")
        if raw_subscription_id is None:
            note = str(desired.get("note") or "")
            match = re.fullmatch(r"CEA VPN subscription ([1-9][0-9]*)", note)
            raw_subscription_id = match.group(1) if match else None
        if raw_subscription_id is None and operation == "disable":
            # Disable needs only the immutable provider username.  Railway does
            # not include a note/subscription id in its minimal disable payload.
            subscription_id = 0
        else:
            try:
                subscription_id = int(raw_subscription_id)
            except (TypeError, ValueError) as exc:
                raise WorkerError("invalid_job_subscription", retryable=False) from exc
            if subscription_id <= 0:
                raise WorkerError("invalid_job_subscription", retryable=False)

        supplied_base = payload.get("subscription_base_url")
        if supplied_base is not None:
            if not isinstance(supplied_base, str):
                raise WorkerError("invalid_job_subscription_base", retryable=False)
            supplied_root = _normalized_subscription_root(
                _validated_https_base(
                    supplied_base, "JOB_SUBSCRIPTION_BASE_URL"
                )
            )
            configured_root = (
                _normalized_subscription_root(subscription_base_url)
                if subscription_base_url
                else supplied_root
            )
            if supplied_root != configured_root:
                raise WorkerError("job_subscription_base_mismatch", retryable=False)

        expire = None if operation in {"disable", "sync"} else _job_expire(desired)
        return cls(
            job_id=job_id,
            lease_token=lease_token,
            operation=operation,
            subscription_id=subscription_id,
            provider_username=username,
            expire=expire,
            data_limit=_validated_data_limit(desired.get("data_limit", 0)),
        )


class VpnWorker:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        railway: Optional[SignedRailwayClient] = None,
        marzban: Optional[LocalMarzbanClient] = None,
    ) -> None:
        self.config = config
        self.railway = railway or SignedRailwayClient(config)
        self.marzban = marzban or LocalMarzbanClient(config)

    def run_once(self) -> bool:
        self.marzban.healthcheck()
        raw_job = self.railway.claim(control_plane_ready=True)
        if raw_job is None:
            return False
        try:
            job = ProvisioningJob.from_payload(
                raw_job,
                subscription_base_url=self.config.subscription_base_url,
            )
        except WorkerError as exc:
            identity = _safe_failure_identity(raw_job)
            if identity is not None:
                self._report_failure(identity, exc)
            LOGGER.error("Rejected invalid provisioning job: %s", exc.code)
            return True

        LOGGER.info(
            "Processing VPN job id=%s operation=%s",
            job.job_id,
            job.operation,
        )
        try:
            result = self._execute(job)
        except WorkerError as exc:
            self._report_failure(job, exc)
            LOGGER.warning(
                "VPN job id=%s operation=%s failed: %s",
                job.job_id,
                job.operation,
                exc.code,
            )
            return True
        except Exception:
            error = WorkerError("unexpected_worker_error")
            self._report_failure(job, error)
            LOGGER.error(
                "VPN job id=%s operation=%s failed: unexpected_worker_error",
                job.job_id,
                job.operation,
            )
            return True

        payload: Dict[str, Any] = {
            "worker_id": self.config.worker_id,
            "job_id": job.job_id,
            "lease_token": job.lease_token,
            "operation": job.operation,
            "provider_status": str(result.get("status") or "active"),
        }
        subscription_url = result.get("subscription_url")
        if isinstance(subscription_url, str) and subscription_url:
            _validate_subscription_url(
                subscription_url, self.config.subscription_base_url
            )
            payload["subscription_url"] = subscription_url
        self.railway.report_result(payload)
        LOGGER.info(
            "Completed VPN job id=%s operation=%s",
            job.job_id,
            job.operation,
        )
        return True

    def run_forever(self, stop_event: threading.Event) -> None:
        backoff = self.config.poll_interval_seconds
        while not stop_event.is_set():
            try:
                worked = self.run_once()
                backoff = self.config.poll_interval_seconds
            except WorkerError as exc:
                LOGGER.warning("Worker poll/report failed: %s", exc.code)
                worked = False
                backoff = min(max(backoff * 2, 5.0), 60.0)
            except Exception:
                LOGGER.error("Worker poll/report failed: unexpected_worker_error")
                worked = False
                backoff = min(max(backoff * 2, 5.0), 60.0)
            delay = 0.1 if worked else backoff
            stop_event.wait(delay)

    def _execute(self, job: ProvisioningJob) -> Dict[str, Any]:
        if job.operation in {"create", "update"}:
            assert job.expire is not None
            return self.marzban.ensure_active(
                username=job.provider_username,
                expire=job.expire,
                subscription_id=job.subscription_id,
                data_limit=job.data_limit,
            )
        if job.operation == "disable":
            return self.marzban.disable(job.provider_username)
        user = self.marzban.get_user(job.provider_username)
        return user or {"status": "disabled"}

    def _report_failure(self, job: Any, error: WorkerError) -> None:
        payload = {
            "worker_id": self.config.worker_id,
            "job_id": int(job.job_id),
            "lease_token": str(job.lease_token),
            "operation": str(job.operation),
            "error": error.code,
            "error_code": error.code,
            "retryable": bool(error.retryable),
        }
        try:
            self.railway.report_failure(payload)
        except WorkerError as report_error:
            LOGGER.warning(
                "Could not report VPN job id=%s failure: %s",
                job.job_id,
                report_error.code,
            )
        except Exception:
            LOGGER.warning(
                "Could not report VPN job id=%s failure: unexpected_worker_error",
                job.job_id,
            )


@dataclass(frozen=True)
class _FailureIdentity:
    job_id: int
    lease_token: str
    operation: str


def _safe_failure_identity(payload: Mapping[str, Any]) -> Optional[_FailureIdentity]:
    try:
        job_id = int(payload.get("job_id") or payload.get("id"))
    except (TypeError, ValueError):
        return None
    lease_token = str(payload.get("lease_token") or "")
    operation = str(payload.get("operation") or "invalid")[:32]
    if job_id <= 0 or not (16 <= len(lease_token) <= 512):
        return None
    return _FailureIdentity(job_id, lease_token, operation)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("VPN_WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = WorkerConfig.from_env()
    except WorkerError as exc:
        LOGGER.error("Worker configuration error: %s", exc.code)
        return 2

    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info("CEA VPN worker starting id=%s", config.worker_id)
    VpnWorker(config).run_forever(stop_event)
    LOGGER.info("CEA VPN worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
