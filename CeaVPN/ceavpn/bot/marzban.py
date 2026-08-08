from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import Any, Dict, List, Mapping, Optional, TypedDict, Union
from urllib.parse import quote, urlsplit

import aiohttp


class MarzbanUserPayload(TypedDict, total=False):
    """Fields accepted by Marzban's create and update user endpoints."""

    username: str
    proxies: Dict[str, Dict[str, Any]]
    inbounds: Dict[str, List[str]]
    expire: int
    data_limit: int
    data_limit_reset_strategy: str
    status: str
    note: str
    on_hold_expire_duration: int
    on_hold_timeout: str
    auto_delete_in_days: int
    next_plan: Dict[str, Any]


class MarzbanError(Exception):
    """Base class for all failures raised by :class:`MarzbanClient`."""


class MarzbanTransportError(MarzbanError):
    """Base class for failures that happen before an HTTP response is read."""

    def __init__(self, method: str, path: str, message: str) -> None:
        self.method = method
        self.path = path
        super().__init__(message)


class MarzbanTimeoutError(MarzbanTransportError):
    """The Marzban request exceeded its configured timeout."""


class MarzbanNetworkError(MarzbanTransportError):
    """The Marzban server could not be reached or the connection failed."""


class MarzbanResponseError(MarzbanError):
    """Marzban returned a successful response with an invalid JSON shape."""


class MarzbanHTTPError(MarzbanError):
    """Base class for non-success HTTP responses from Marzban."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        detail: str,
        payload: Any = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        self.payload = payload
        super().__init__(f"Marzban API returned HTTP {status}: {detail}")


class MarzbanAuthenticationError(MarzbanHTTPError):
    """Credentials or an access token were rejected."""


class MarzbanPermissionError(MarzbanHTTPError):
    """The authenticated Marzban admin cannot perform the operation."""


class MarzbanNotFoundError(MarzbanHTTPError):
    """The requested Marzban resource does not exist."""


class MarzbanConflictError(MarzbanHTTPError):
    """The operation conflicts with an existing Marzban resource."""


TimeoutValue = Union[float, aiohttp.ClientTimeout]


class MarzbanClient:
    """Small asynchronous client for the Marzban v0.8 REST API.

    Access tokens are cached and refreshed once after a ``401``. ``create_user``
    is intentionally idempotent: if Marzban reports that the username already
    exists, the existing user is read and then updated with the desired fields.

    A supplied ``aiohttp.ClientSession`` remains owned by the caller. Otherwise
    the client lazily creates and closes its own session.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: TimeoutValue = 10.0,
        session: Optional[aiohttp.ClientSession] = None,
        allow_insecure_http: bool = False,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("base_url must not be empty")
        parsed_base_url = urlsplit(normalized_base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if (
            parsed_base_url.scheme == "http"
            and not allow_insecure_http
            and not self._is_loopback_host(parsed_base_url.hostname)
        ):
            raise ValueError(
                "Marzban requires HTTPS for non-local addresses; "
                "set allow_insecure_http=True only for an explicitly trusted network"
            )
        if not username:
            raise ValueError("username must not be empty")
        if not password:
            raise ValueError("password must not be empty")

        if isinstance(timeout, aiohttp.ClientTimeout):
            request_timeout = timeout
        else:
            if timeout <= 0:
                raise ValueError("timeout must be greater than zero")
            request_timeout = aiohttp.ClientTimeout(total=float(timeout))

        self.base_url = normalized_base_url
        self.username = username
        self.password = password
        self._timeout = request_timeout
        self._session = session
        self._owns_session = session is None
        self._access_token: Optional[str] = None
        self._token_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "MarzbanClient":
        await self._get_session()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the internally-created HTTP session, if there is one."""

        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached access token or authenticate with Marzban."""

        async with self._token_lock:
            if self._access_token and not force_refresh:
                return self._access_token
            self._access_token = await self._fetch_token()
            return self._access_token

    async def get_user(self, username: str) -> Dict[str, Any]:
        """Fetch one Marzban user by username."""

        safe_username = self._encoded_username(username)
        return await self._request_json("GET", f"/api/user/{safe_username}")

    async def update_user(
        self,
        username: str,
        user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Update one Marzban user and return the server representation."""

        safe_username = self._encoded_username(username)
        payload = dict(user)
        # Marzban identifies the user by its path and does not allow renaming.
        payload.pop("username", None)
        return await self._request_json(
            "PUT",
            f"/api/user/{safe_username}",
            json_body=payload,
        )

    async def create_user(self, user: Mapping[str, Any]) -> Dict[str, Any]:
        """Create a user, or converge an already-existing user to the payload.

        A ``409 User already exists`` can also be observed when the first create
        succeeded but its response was lost. Reading before updating preserves
        server-generated proxy credentials in that retry scenario.
        """

        payload = dict(user)
        username = payload.get("username")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("user payload must contain a non-empty username")

        try:
            return await self._request_json(
                "POST",
                "/api/user",
                json_body=payload,
            )
        except MarzbanConflictError:
            existing = await self.get_user(username)
            update_payload = self._payload_for_existing_user(payload, existing)
            return await self.update_user(username, update_payload)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise MarzbanError("MarzbanClient is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        if self._session.closed:
            raise MarzbanError("The aiohttp session used by MarzbanClient is closed")
        return self._session

    async def _fetch_token(self) -> str:
        status, body, raw_body = await self._send(
            "POST",
            "/api/admin/token",
            form_body={
                "username": self.username,
                "password": self.password,
                "grant_type": "password",
            },
        )
        if not 200 <= status < 300:
            raise self._http_error(
                method="POST",
                path="/api/admin/token",
                status=status,
                body=body,
                raw_body=raw_body,
                authentication_request=True,
            )
        if not isinstance(body, Mapping):
            raise MarzbanResponseError(
                "Marzban token endpoint returned a non-object JSON response"
            )
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise MarzbanResponseError(
                "Marzban token response does not contain access_token"
            )
        return token

    async def _refresh_after_unauthorized(self, stale_token: str) -> str:
        # Avoid a token-refresh stampede if several requests fail together.
        async with self._token_lock:
            if self._access_token and self._access_token != stale_token:
                return self._access_token
            self._access_token = await self._fetch_token()
            return self._access_token

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = await self.get_token()
        status, body, raw_body = await self._send(
            method,
            path,
            token=token,
            json_body=json_body,
        )

        if status == 401:
            token = await self._refresh_after_unauthorized(token)
            status, body, raw_body = await self._send(
                method,
                path,
                token=token,
                json_body=json_body,
            )

        if not 200 <= status < 300:
            raise self._http_error(
                method=method,
                path=path,
                status=status,
                body=body,
                raw_body=raw_body,
            )
        if not isinstance(body, Mapping):
            raise MarzbanResponseError(
                f"Marzban {method} {path} returned a non-object JSON response"
            )
        return dict(body)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        form_body: Optional[Mapping[str, str]] = None,
    ) -> tuple[int, Any, str]:
        session = await self._get_session()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request_kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self._timeout,
        }
        if json_body is not None:
            request_kwargs["json"] = dict(json_body)
        if form_body is not None:
            request_kwargs["data"] = dict(form_body)

        try:
            async with session.request(
                method,
                self._url(path),
                **request_kwargs,
            ) as response:
                raw_body = await response.text()
                return response.status, self._decode_body(raw_body), raw_body
        except asyncio.TimeoutError as exc:
            raise MarzbanTimeoutError(
                method,
                path,
                f"Marzban {method} {path} timed out",
            ) from exc
        except aiohttp.ClientError as exc:
            raise MarzbanNetworkError(
                method,
                path,
                f"Marzban {method} {path} failed due to a network error",
            ) from exc

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _encoded_username(username: str) -> str:
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must not be empty")
        return quote(username, safe="")

    @staticmethod
    def _is_loopback_host(hostname: str) -> bool:
        normalized = hostname.rstrip(".").lower()
        if normalized == "localhost" or normalized.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _decode_body(raw_body: str) -> Any:
        if not raw_body.strip():
            return {}
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _payload_for_existing_user(
        desired: Mapping[str, Any],
        existing: Mapping[str, Any],
    ) -> Dict[str, Any]:
        update_payload = dict(desired)
        update_payload.pop("username", None)

        desired_proxies = desired.get("proxies")
        existing_proxies = existing.get("proxies")
        if isinstance(desired_proxies, Mapping):
            merged_proxies: Dict[str, Any] = {}
            current = existing_proxies if isinstance(existing_proxies, Mapping) else {}
            for protocol, desired_settings in desired_proxies.items():
                current_settings = current.get(protocol, {})
                if isinstance(current_settings, Mapping) and isinstance(
                    desired_settings, Mapping
                ):
                    settings = dict(current_settings)
                    settings.update(desired_settings)
                    merged_proxies[str(protocol)] = settings
                else:
                    merged_proxies[str(protocol)] = desired_settings
            update_payload["proxies"] = merged_proxies

        return update_payload

    @classmethod
    def _http_error(
        cls,
        *,
        method: str,
        path: str,
        status: int,
        body: Any,
        raw_body: str,
        authentication_request: bool = False,
    ) -> MarzbanHTTPError:
        detail = cls._error_detail(body, raw_body)
        kwargs = {
            "method": method,
            "path": path,
            "status": status,
            "detail": detail,
            "payload": body,
        }
        if status == 401 or (authentication_request and status in {400, 403, 422}):
            return MarzbanAuthenticationError(**kwargs)
        if status == 403:
            return MarzbanPermissionError(**kwargs)
        if status == 404:
            return MarzbanNotFoundError(**kwargs)
        if status == 409:
            return MarzbanConflictError(**kwargs)
        return MarzbanHTTPError(**kwargs)

    @staticmethod
    def _error_detail(body: Any, raw_body: str) -> str:
        detail: Any = body.get("detail") if isinstance(body, Mapping) else None
        if detail is None:
            detail = raw_body.strip() or "No response detail"
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                detail = str(detail)
        # Avoid propagating unexpectedly large HTML/proxy error pages.
        return detail[:500]


__all__ = [
    "MarzbanAuthenticationError",
    "MarzbanClient",
    "MarzbanConflictError",
    "MarzbanError",
    "MarzbanHTTPError",
    "MarzbanNetworkError",
    "MarzbanNotFoundError",
    "MarzbanPermissionError",
    "MarzbanResponseError",
    "MarzbanTimeoutError",
    "MarzbanTransportError",
    "MarzbanUserPayload",
]
