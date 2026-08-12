from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from ceavpn.config import Settings
from ceavpn.database import Database
from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceavpn.services.vpn import MARZBAN_WHITELIST_PROFILE_VERSION


TOKEN_RE = re.compile(r"(?P<id>[1-9][0-9]*)\.(?P<signature>[0-9a-f]{64})")
PROFILE_BASE_KEYS = frozenset({"remark", "address", "port", "sni", "path"})
WS_PROFILE_KEYS = PROFILE_BASE_KEYS | {"host", "transport", "security"}
XHTTP_PROFILE_KEYS = PROFILE_BASE_KEYS | {
    "transport",
    "security",
    "pbk",
    "public_key",
    "sid",
    "fingerprint",
    "qualification_url",
    "qualification_fingerprint",
    "server_code",
}
PROFILE_KEYS = WS_PROFILE_KEYS | XHTTP_PROFILE_KEYS
DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
XHTTP_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]{0,1023}")
REALITY_PUBLIC_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}")
REALITY_SHORT_ID_RE = re.compile(r"(?:[0-9A-Fa-f]{2}){1,8}")
QUALIFICATION_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
QUALIFICATION_STATUS_PATH = "/.well-known/ceavpn-whitelist-status"
QUALIFICATION_STATUS_SERVICE = "ceavpn-whitelist-gate-v1"
QUALIFICATION_STATUS_KEYS = frozenset(
    {"service", "status", "config_fingerprint", "valid_until"}
)
QUALIFICATION_STATUS_MAX_BYTES = 4096
QUALIFICATION_MAX_FUTURE = timedelta(days=7)
XHTTP_MODE = "auto"
XHTTP_EXTRA = {
    "scMaxEachPostBytes": 1000000,
    "scMaxConcurrentPosts": 100,
    "scMinPostsIntervalMs": 30,
    "xPaddingBytes": "100-1000",
    "noGRPCHeader": False,
}
QUALIFICATION_PROFILE_FIELDS = (
    "address",
    "port",
    "transport",
    "security",
    "path",
    "sni",
    "pbk",
    "sid",
    "fingerprint",
    "qualification_url",
    "server_code",
)
QUALIFICATION_TIMEOUT = ClientTimeout(
    total=3,
    connect=2,
    sock_connect=2,
    sock_read=2,
)
UTLS_FINGERPRINTS = frozenset(
    {
        "chrome",
        "firefox",
        "safari",
        "ios",
        "android",
        "edge",
        "360",
        "qq",
        "random",
        "randomized",
    }
)
FORWARDED_HEADERS = (
    "content-disposition",
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "support-url",
)


def _profile_string(
    item: Mapping[str, Any],
    key: str,
    *,
    maximum_length: int,
) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError("Invalid VPN extra profile field")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Invalid VPN extra profile values")
    value = value.strip()
    if not value or len(value) > maximum_length:
        raise ValueError("Invalid VPN extra profile values")
    return value


def _valid_dns_name(value: str) -> bool:
    if len(value) > 253 or value.endswith("."):
        return False
    labels = value.split(".")
    return all(DNS_LABEL_RE.fullmatch(label) for label in labels)


def _valid_address(value: str) -> bool:
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return _valid_dns_name(value)
    return True


def _valid_transport_path(value: str) -> bool:
    if (
        not XHTTP_PATH_RE.fullmatch(value)
        or value.startswith("//")
        or "\\" in value
        or "%" in value
    ):
        return False
    return not any(segment in {".", ".."} for segment in value.split("/"))


def _valid_qualification_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
        or not _valid_dns_name(hostname)
        or port != 8443
        or parsed.path != QUALIFICATION_STATUS_PATH
        or parsed.query
        or parsed.fragment
    ):
        return False
    return value == f"https://{hostname}:8443{QUALIFICATION_STATUS_PATH}"


def _profile_transport(item: Mapping[str, Any]) -> tuple[str, str]:
    transport_value = item.get("transport")
    security_value = item.get("security")
    if transport_value is None and security_value is None:
        return "ws", "tls"
    if not isinstance(transport_value, str) or not isinstance(
        security_value, str
    ):
        raise ValueError("Invalid VPN extra profile transport")
    if any(
        unicodedata.category(char).startswith("C")
        for value in (transport_value, security_value)
        for char in value
    ):
        raise ValueError("Invalid VPN extra profile transport")
    transport = transport_value.strip().lower()
    security = security_value.strip().lower()
    if (transport, security) not in {("ws", "tls"), ("xhttp", "reality")}:
        raise ValueError("Invalid VPN extra profile transport")
    return transport, security


def qualification_profile_fingerprint(profile: Mapping[str, Any]) -> str:
    payload = {key: profile[key] for key in QUALIFICATION_PROFILE_FIELDS}
    payload["mode"] = XHTTP_MODE
    payload["extra"] = XHTTP_EXTRA
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def delivery_base_url(settings: Settings) -> str:
    base_url = (settings.vpn_delivery_base_url or settings.app_base_url).rstrip("/")
    # The Railway delivery endpoint is optional.  Do not expose it in client
    # links unless signing is actually enabled: otherwise the UI must fall
    # back to the reachable VPS subscription host.
    if not base_url.startswith("https://"):
        return ""
    if len(settings.vpn_delivery_signing_secret.encode("utf-8")) < 32:
        return ""
    return base_url


def _signature(
    subscription_id: int,
    provider_username: str,
    secret: str,
) -> str:
    message = f"{int(subscription_id)}:{provider_username}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def delivery_subscription_url(
    subscription: Mapping[str, Any] | None,
    settings: Settings,
) -> str:
    if not subscription:
        return ""
    original = str(subscription.get("subscription_url") or "")
    base_url = delivery_base_url(settings)
    secret = settings.vpn_delivery_signing_secret
    provider_username = str(subscription.get("provider_username") or "")
    try:
        subscription_id = int(subscription["id"])
    except (KeyError, TypeError, ValueError):
        return original
    if (
        not base_url.startswith("https://")
        or len(secret.encode("utf-8")) < 32
        or not provider_username
    ):
        return original
    signature = _signature(subscription_id, provider_username, secret)
    return f"{base_url}/sub/{subscription_id}.{signature}"


def with_delivery_subscription(
    subscription: Mapping[str, Any] | None,
    settings: Settings,
) -> dict[str, Any] | None:
    if subscription is None:
        return None
    result = dict(subscription)
    result["subscription_url"] = delivery_subscription_url(result, settings)
    return result


def parse_extra_profiles(raw: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("VPN_EXTRA_PROFILES_JSON must be valid JSON") from exc
    if not isinstance(payload, list) or len(payload) > 32:
        raise ValueError("VPN_EXTRA_PROFILES_JSON must be a short JSON array")
    profiles: list[dict[str, Any]] = []
    remarks: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Invalid VPN extra profile")
        if not set(item).issubset(PROFILE_KEYS):
            raise ValueError("Invalid VPN extra profile field")
        transport, security = _profile_transport(item)
        allowed_keys = (
            XHTTP_PROFILE_KEYS if transport == "xhttp" else WS_PROFILE_KEYS
        )
        if not set(item).issubset(allowed_keys):
            raise ValueError("Invalid VPN extra profile field")
        port = item.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("Invalid VPN extra profile port")
        profile = {
            "remark": _profile_string(item, "remark", maximum_length=128),
            "address": _profile_string(item, "address", maximum_length=253),
            "port": port,
            "sni": _profile_string(item, "sni", maximum_length=253),
            "path": _profile_string(item, "path", maximum_length=1024),
            "transport": transport,
            "security": security,
        }
        if (
            profile["remark"] in remarks
            or not _valid_address(profile["address"])
            or not _valid_dns_name(profile["sni"])
            or not 1 <= port <= 65535
        ):
            raise ValueError("Invalid VPN extra profile values")
        if transport == "ws":
            profile["host"] = _profile_string(
                item, "host", maximum_length=253
            )
            if (
                not _valid_dns_name(profile["host"])
                or not re.fullmatch(r"/ws-[0-9a-f]{48}", profile["path"])
            ):
                raise ValueError("Invalid VPN extra profile values")
        else:
            has_pbk = "pbk" in item
            has_public_key = "public_key" in item
            if has_pbk == has_public_key:
                raise ValueError("Invalid VPN extra profile public key")
            public_key_field = "pbk" if has_pbk else "public_key"
            profile["pbk"] = _profile_string(
                item, public_key_field, maximum_length=43
            )
            profile["sid"] = _profile_string(
                item, "sid", maximum_length=16
            ).lower()
            profile["fingerprint"] = _profile_string(
                item, "fingerprint", maximum_length=32
            ).lower()
            profile["qualification_url"] = _profile_string(
                item, "qualification_url", maximum_length=320
            )
            profile["qualification_fingerprint"] = _profile_string(
                item, "qualification_fingerprint", maximum_length=64
            )
            profile["server_code"] = _profile_string(
                item, "server_code", maximum_length=32
            ).lower()
            expected_qualification_fingerprint = (
                qualification_profile_fingerprint(profile)
            )
            if (
                not _valid_transport_path(profile["path"])
                or not REALITY_PUBLIC_KEY_RE.fullmatch(profile["pbk"])
                or not REALITY_SHORT_ID_RE.fullmatch(profile["sid"])
                or profile["fingerprint"] not in UTLS_FINGERPRINTS
                or not _valid_qualification_url(
                    profile["qualification_url"]
                )
                or not QUALIFICATION_FINGERPRINT_RE.fullmatch(
                    profile["qualification_fingerprint"]
                )
                or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{1,31}",
                    profile["server_code"],
                )
                or not hmac.compare_digest(
                    profile["qualification_fingerprint"],
                    expected_qualification_fingerprint,
                )
            ):
                raise ValueError("Invalid VPN extra profile values")
        remarks.add(profile["remark"])
        profiles.append(profile)
    return tuple(profiles)


def _qualification_status_is_current(
    payload: Any,
    *,
    expected_fingerprint: str,
    now: datetime,
) -> bool:
    if now.tzinfo is None:
        return False
    if (
        not isinstance(payload, dict)
        or set(payload) != QUALIFICATION_STATUS_KEYS
        or not all(isinstance(value, str) for value in payload.values())
        or payload["service"] != QUALIFICATION_STATUS_SERVICE
        or payload["status"] != "passed"
        or not QUALIFICATION_FINGERPRINT_RE.fullmatch(
            payload["config_fingerprint"]
        )
        or not hmac.compare_digest(
            payload["config_fingerprint"], expected_fingerprint
        )
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            payload["valid_until"],
        )
    ):
        return False
    try:
        valid_until = datetime.strptime(
            payload["valid_until"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    current = now.astimezone(timezone.utc)
    return current < valid_until <= current + QUALIFICATION_MAX_FUTURE


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def _qualification_profile_is_current(
    session: ClientSession,
    profile: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    try:
        async with session.get(
            str(profile["qualification_url"]),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
            timeout=QUALIFICATION_TIMEOUT,
        ) as response:
            if response.status != 200:
                return False
            media_type = response.headers.get("Content-Type", "")
            if (
                media_type.partition(";")[0].strip().lower()
                != "application/json"
            ):
                return False
            if response.headers.get("Content-Encoding", "").strip().lower() not in {
                "",
                "identity",
            }:
                return False
            if (
                response.content_length is not None
                and response.content_length > QUALIFICATION_STATUS_MAX_BYTES
            ):
                return False
            body = bytearray()
            async for chunk in response.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > QUALIFICATION_STATUS_MAX_BYTES:
                    return False
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except Exception:
        return False
    return _qualification_status_is_current(
        payload,
        expected_fingerprint=str(profile["qualification_fingerprint"]),
        now=now,
    )


async def qualified_extra_profiles(
    session: ClientSession,
    profiles: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[Mapping[str, Any], ...]:
    current = now or datetime.now(timezone.utc)
    xhttp_profiles = [
        profile
        for profile in profiles
        if (
            str(profile.get("transport") or "ws"),
            str(profile.get("security") or "tls"),
        )
        == ("xhttp", "reality")
    ]
    results = iter(
        await asyncio.gather(
            *(
                _qualification_profile_is_current(
                    session,
                    profile,
                    now=current,
                )
                for profile in xhttp_profiles
            )
        )
    )
    eligible: list[Mapping[str, Any]] = []
    for profile in profiles:
        transport = str(profile.get("transport") or "ws")
        security = str(profile.get("security") or "tls")
        if (transport, security) != ("xhttp", "reality") or next(results):
            eligible.append(profile)
    return tuple(eligible)


def replica_ready_extra_profiles(
    db: Database,
    repository: VpnSubscriptionRepository,
    profiles: Sequence[Mapping[str, Any]],
    *,
    subscription_id: int,
    worker_health_max_age_seconds: int,
    now: datetime | None = None,
) -> tuple[Mapping[str, Any], ...]:
    non_xhttp = tuple(
        profile
        for profile in profiles
        if (
            str(profile.get("transport") or "ws"),
            str(profile.get("security") or "tls"),
        )
        != ("xhttp", "reality")
    )
    if (
        subscription_id <= 0
        or worker_health_max_age_seconds <= 0
    ):
        return non_xhttp
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return non_xhttp
    current = current.astimezone(timezone.utc)
    healthy_after = (
        current - timedelta(seconds=worker_health_max_age_seconds)
    ).isoformat()
    active_at = current.isoformat()
    eligible: list[Mapping[str, Any]] = []
    try:
        with db.transaction() as conn:
            for profile in profiles:
                transport = str(profile.get("transport") or "ws")
                security = str(profile.get("security") or "tls")
                if (transport, security) != ("xhttp", "reality"):
                    eligible.append(profile)
                    continue
                if repository.has_completed_server_replica(
                    conn,
                    subscription_id=subscription_id,
                    server_code=str(profile["server_code"]),
                    profile_version=MARZBAN_WHITELIST_PROFILE_VERSION,
                    healthy_after=healthy_after,
                    active_at=active_at,
                ):
                    eligible.append(profile)
    except Exception:
        return non_xhttp
    return tuple(eligible)


def _decode_subscription(body: bytes) -> tuple[list[str], bool]:
    text = body.decode("utf-8").strip()
    if not text:
        raise ValueError("Empty VPN subscription")
    if text.startswith("vless://"):
        return [line for line in text.splitlines() if line.strip()], False
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("Invalid VPN subscription encoding") from exc
    lines = [line for line in decoded.splitlines() if line.strip()]
    if not lines or any(not line.startswith("vless://") for line in lines):
        raise ValueError("Invalid VPN subscription profiles")
    return lines, True


def _profile_uri(provider_uuid: str, profile: Mapping[str, Any]) -> str:
    transport = str(profile.get("transport") or "ws")
    security = str(profile.get("security") or "tls")
    if transport == "xhttp" and security == "reality":
        query_parameters = (
            ("encryption", "none"),
            ("type", "xhttp"),
            ("security", "reality"),
            ("path", profile["path"]),
            ("sni", profile["sni"]),
            ("fp", profile["fingerprint"]),
            ("pbk", profile["pbk"]),
            ("sid", profile["sid"]),
            ("mode", XHTTP_MODE),
            (
                "extra",
                json.dumps(
                    XHTTP_EXTRA,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            ),
            ("headerType", ""),
        )
    elif transport == "ws" and security == "tls":
        query_parameters = (
            ("encryption", "none"),
            ("security", "tls"),
            ("sni", profile["sni"]),
            ("fp", "chrome"),
            ("type", "ws"),
            ("host", profile["host"]),
            ("path", profile["path"]),
        )
    else:
        raise ValueError("Invalid VPN extra profile transport")
    query = urlencode(query_parameters)
    address = str(profile["address"])
    try:
        is_ipv6 = ipaddress.ip_address(address).version == 6
    except ValueError:
        is_ipv6 = False
    rendered_address = f"[{address}]" if is_ipv6 else address
    return (
        f"vless://{provider_uuid}@{rendered_address}:{profile['port']}"
        f"?{query}#{quote(str(profile['remark']), safe='')}"
    )


def merge_subscription_profiles(
    body: bytes,
    *,
    provider_uuid: str,
    profiles: Sequence[Mapping[str, Any]],
) -> bytes:
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        provider_uuid.lower(),
    ):
        raise ValueError("Invalid VPN provider UUID")
    lines, was_base64 = _decode_subscription(body)
    for profile in profiles:
        uri = _profile_uri(provider_uuid, profile)
        address = str(profile["address"])
        try:
            is_ipv6 = ipaddress.ip_address(address).version == 6
        except ValueError:
            is_ipv6 = False
        rendered_address = f"[{address}]" if is_ipv6 else address
        marker = f"@{rendered_address}:{profile['port']}?"
        path_marker = quote(str(profile["path"]), safe="")
        transport_marker = f"type={profile.get('transport') or 'ws'}"
        security_marker = f"security={profile.get('security') or 'tls'}"
        if not any(
            marker in line
            and path_marker in line
            and transport_marker in line
            and security_marker in line
            for line in lines
        ):
            lines.append(uri)
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    return base64.b64encode(rendered) if was_base64 else rendered


def _landing_html(subscription_url: str, *, client: str) -> str:
    encoded_url = quote(subscription_url, safe="")
    if client == "v2box":
        deep_link = f"v2box://install-sub?url={encoded_url}&name=CEA%20VPN"
        title = "Подключение через V2Box"
    else:
        deep_link = f"happ://add/{subscription_url}"
        title = "Подключение через Happ"
    safe_link = escape(deep_link, quote=True)
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{title}</title><style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#fff;color:#171717;padding:24px;box-sizing:border-box}"
        "main{width:min(100%,460px);padding:36px 32px;border:1px solid #e8e8e8;"
        "border-radius:20px;background:#fff;text-align:center;box-shadow:0 12px 35px "
        "rgba(0,0,0,.06)}h1{font-size:25px;line-height:1.25;margin:0 0 12px;}"
        "p{margin:0;color:#666;line-height:1.55}a{display:inline-block;margin-top:24px;"
        "padding:14px 20px;border-radius:12px;background:#111;color:#fff;"
        "text-decoration:none;font-weight:700}small{display:block;margin-top:16px;color:#999;"
        "line-height:1.45}</style></head><body><main>"
        f"<h1>{title}</h1><p>Вы остаётесь на этой странице. Когда будете готовы, "
        "нажмите кнопку — подписка откроется в приложении.</p>"
        f"<a href=\"{safe_link}\">Открыть {('V2Box' if client == 'v2box' else 'Happ')}</a>"
        "<small>Если приложение не установлено, сначала установите его, затем вернитесь сюда.</small>"
        "</main></body></html>"
    )


def expired_subscription_response() -> web.Response:
    remark = "⚠️ Подписка истекла. Продлите в боте"
    uri = (
        "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1"
        "?type=ws&security=none#"
        + quote(remark, safe="")
    )
    body = base64.b64encode((uri + "\n").encode("utf-8"))
    headers = {
        "profile-title": "CEA VPN (Подписка истекла)",
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "routing-enable": "0",
    }
    return web.Response(
        body=body,
        content_type="text/plain",
        charset="utf-8",
        headers=headers,
    )


def device_limit_exceeded_response(bot_username: str = "ceavpn_bot") -> web.Response:
    username = bot_username.strip().lstrip("@")
    zero_uuid = "00000000-0000-0000-0000-000000000000"
    remarks = (
        "🔴 Лимит устройств исчерпан",
        f"👉 Докупить устройства можно в боте @{username}",
    )
    links = [
        (
            f"vless://{zero_uuid}@127.0.0.1:{index}"
            "?type=ws&security=none#"
            + quote(remark, safe="")
        )
        for index, remark in enumerate(remarks, start=1)
    ]
    body = base64.b64encode(("\n".join(links) + "\n").encode("utf-8"))
    headers = {
        "profile-title": "CEA VPN (Лимит устройств исчерпан)",
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "routing-enable": "0",
    }
    return web.Response(
        body=body,
        content_type="text/plain",
        charset="utf-8",
        headers=headers,
    )


def is_subscription_active(subscription: Mapping[str, Any] | None) -> bool:
    if not subscription:
        return False
    status = str(subscription.get("status") or "active")
    if status != "active":
        return False
    ends_at_str = subscription.get("ends_at")
    if ends_at_str:
        try:
            dt = datetime.fromisoformat(str(ends_at_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= datetime.now(timezone.utc):
                return False
        except Exception:
            return False
    return True


def register_vpn_subscription_delivery_routes(
    app: web.Application,
    *,
    db: Database,
    settings: Settings,
) -> None:
    profiles = parse_extra_profiles(settings.vpn_extra_profiles_json)
    repository = VpnSubscriptionRepository()

    def resolve(token: str, *, allow_inactive: bool = False) -> dict[str, Any] | None:
        match = TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        subscription_id = int(match.group("id"))
        with db.transaction() as conn:
            subscription = repository.get_by_id(conn, subscription_id)
        if subscription is None:
            return None
        secret = settings.vpn_delivery_signing_secret
        expected = _signature(
            subscription_id,
            str(subscription.get("provider_username") or ""),
            secret,
        )
        if len(secret.encode("utf-8")) < 32 or not hmac.compare_digest(
            expected, match.group("signature")
        ):
            return None
        if not allow_inactive and not is_subscription_active(subscription):
            return None
        return subscription

    async def merged_subscription(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        subscription = resolve(token)
        if subscription is None:
            return expired_subscription_response()
        upstream_url = str(subscription.get("subscription_url") or "")
        parsed = urlsplit(upstream_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or not re.fullmatch(r"/sub/[A-Za-z0-9._~-]{1,160}/?", parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            return expired_subscription_response()
        timeout = ClientTimeout(total=15, connect=5)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    upstream_url,
                    headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
                    allow_redirects=False,
                ) as upstream:
                    body = await upstream.read()
                    if upstream.status != 200 or len(body) > 512 * 1024:
                        return expired_subscription_response()
                    headers = {
                        name: upstream.headers[name]
                        for name in FORWARDED_HEADERS
                        if name in upstream.headers
                    }
                replica_profiles = replica_ready_extra_profiles(
                    db,
                    repository,
                    profiles,
                    subscription_id=int(subscription["id"]),
                    worker_health_max_age_seconds=(
                        settings.vpn_worker_health_max_age_seconds
                    ),
                )
                eligible_profiles = await qualified_extra_profiles(
                    session,
                    replica_profiles,
                )
            merged = merge_subscription_profiles(
                body,
                provider_uuid=str(subscription.get("provider_uuid") or ""),
                profiles=eligible_profiles,
            )
        except Exception:
            return expired_subscription_response()
        headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "routing-enable": "0",
            }
        )
        return web.Response(
            body=merged,
            content_type="text/plain",
            charset="utf-8",
            headers=headers,
        )

    async def landing(request: web.Request) -> web.Response:
        subscription = resolve(request.match_info["token"], allow_inactive=True)
        if subscription is None:
            raise web.HTTPNotFound()
        subscription_url = delivery_subscription_url(subscription, settings)
        client = request.match_info["client"]
        return web.Response(
            text=_landing_html(subscription_url, client=client),
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
            },
        )

    app.router.add_get("/sub/{token}", merged_subscription)
    app.router.add_get("/{client:happ|v2box|connect}/{token}", landing)


__all__ = [
    "delivery_base_url",
    "delivery_subscription_url",
    "expired_subscription_response",
    "is_subscription_active",
    "merge_subscription_profiles",
    "parse_extra_profiles",
    "qualification_profile_fingerprint",
    "qualified_extra_profiles",
    "replica_ready_extra_profiles",
    "register_vpn_subscription_delivery_routes",
    "with_delivery_subscription",
]
