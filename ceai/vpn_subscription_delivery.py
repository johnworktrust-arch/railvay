from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from html import escape
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from ceai.config import Settings
from ceai.database import Database
from ceai.repositories.vpn_subscriptions import VpnSubscriptionRepository


TOKEN_RE = re.compile(r"(?P<id>[1-9][0-9]*)\.(?P<signature>[0-9a-f]{64})")
PROFILE_KEYS = ("remark", "address", "port", "sni", "host", "path")
FORWARDED_HEADERS = (
    "content-disposition",
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "support-url",
)


def delivery_base_url(settings: Settings) -> str:
    return (settings.vpn_delivery_base_url or settings.app_base_url).rstrip("/")


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
        profile = {key: item.get(key) for key in PROFILE_KEYS}
        if not isinstance(profile["port"], int):
            raise ValueError("Invalid VPN extra profile port")
        for key in ("remark", "address", "sni", "host", "path"):
            if not isinstance(profile[key], str):
                raise ValueError("Invalid VPN extra profile field")
            profile[key] = profile[key].strip()
        if (
            not profile["remark"]
            or profile["remark"] in remarks
            or not profile["address"]
            or not profile["sni"]
            or not profile["host"]
            or not 1 <= profile["port"] <= 65535
            or not re.fullmatch(r"/ws-[0-9a-f]{48}", profile["path"])
        ):
            raise ValueError("Invalid VPN extra profile values")
        remarks.add(profile["remark"])
        profiles.append(profile)
    return tuple(profiles)


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
    query = urlencode(
        {
            "encryption": "none",
            "security": "tls",
            "sni": profile["sni"],
            "fp": "chrome",
            "type": "ws",
            "host": profile["host"],
            "path": profile["path"],
        }
    )
    return (
        f"vless://{provider_uuid}@{profile['address']}:{profile['port']}"
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
        marker = f"@{profile['address']}:{profile['port']}?"
        path_marker = quote(str(profile["path"]), safe="")
        if not any(marker in line and path_marker in line for line in lines):
            lines.append(uri)
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    return base64.b64encode(rendered) if was_base64 else rendered


def _landing_html(subscription_url: str, *, client: str) -> str:
    encoded_url = quote(subscription_url, safe="")
    if client == "v2box":
        deep_link = f"v2box://install-sub?url={encoded_url}&name=CEA%20VPN"
        title = "Открываем V2Box"
    else:
        deep_link = f"happ://add/{subscription_url}"
        title = "Открываем Happ"
    safe_link = escape(deep_link, quote=True)
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<meta http-equiv=\"refresh\" content=\"0;url={safe_link}\">"
        f"<title>{title}</title><style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "font-family:system-ui,sans-serif;background:#0b1020;color:#f8fafc;"
        "padding:24px}main{max-width:520px;padding:28px;border:1px solid #27314b;"
        "border-radius:22px;background:#121a2d;text-align:center}"
        "a{display:inline-block;margin-top:12px;padding:14px 20px;border-radius:13px;"
        "background:#6d5dfc;color:white;text-decoration:none;font-weight:750}"
        "</style></head><body><main>"
        f"<h1>{title}…</h1><p>Если приложение не открылось автоматически, "
        f"нажмите кнопку ниже.</p><a href=\"{safe_link}\">{title}</a>"
        f"</main><script>window.location.replace({json.dumps(deep_link)});"
        "</script></body></html>"
    )


def register_vpn_subscription_delivery_routes(
    app: web.Application,
    *,
    db: Database,
    settings: Settings,
) -> None:
    profiles = parse_extra_profiles(settings.vpn_extra_profiles_json)
    repository = VpnSubscriptionRepository()

    def resolve(token: str) -> dict[str, Any]:
        match = TOKEN_RE.fullmatch(token)
        if match is None:
            raise web.HTTPNotFound()
        subscription_id = int(match.group("id"))
        with db.transaction() as conn:
            subscription = repository.get_by_id(conn, subscription_id)
        if subscription is None:
            raise web.HTTPNotFound()
        secret = settings.vpn_delivery_signing_secret
        expected = _signature(
            subscription_id,
            str(subscription.get("provider_username") or ""),
            secret,
        )
        if len(secret.encode("utf-8")) < 32 or not hmac.compare_digest(
            expected, match.group("signature")
        ):
            raise web.HTTPNotFound()
        return subscription

    async def merged_subscription(request: web.Request) -> web.Response:
        subscription = resolve(request.match_info["token"])
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
            raise web.HTTPServiceUnavailable(text="VPN subscription is unavailable")
        timeout = ClientTimeout(total=15, connect=5)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(
                upstream_url,
                headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
                allow_redirects=False,
            ) as upstream:
                body = await upstream.read()
                if upstream.status != 200 or len(body) > 512 * 1024:
                    raise web.HTTPBadGateway(text="VPN subscription is unavailable")
                headers = {
                    name: upstream.headers[name]
                    for name in FORWARDED_HEADERS
                    if name in upstream.headers
                }
        try:
            merged = merge_subscription_profiles(
                body,
                provider_uuid=str(subscription.get("provider_uuid") or ""),
                profiles=profiles,
            )
        except ValueError as exc:
            raise web.HTTPBadGateway(
                text="VPN subscription is unavailable"
            ) from exc
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
        subscription = resolve(request.match_info["token"])
        subscription_url = delivery_subscription_url(subscription, settings)
        client = request.match_info["client"]
        if client == "connect":
            client = "happ"
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
    "merge_subscription_profiles",
    "parse_extra_profiles",
    "register_vpn_subscription_delivery_routes",
    "with_delivery_subscription",
]
