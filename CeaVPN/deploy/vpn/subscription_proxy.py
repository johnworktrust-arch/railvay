#!/usr/bin/env python3
from __future__ import annotations

import base64
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8010
UPSTREAM_BASE_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 512 * 1024
SUBSCRIPTION_PATH_RE = re.compile(r"/sub/[A-Za-z0-9._~-]{1,160}/?")
FORWARDED_HEADERS = {
    "content-disposition",
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "support-url",
}


def expiration_from_headers(headers: Mapping[str, str]) -> int:
    value = headers.get("subscription-userinfo", "")
    match = re.search(r"(?:^|;)\s*expire=(\d+)(?:\s*;|$)", value)
    return int(match.group(1)) if match else 0


def subscription_has_expired(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> bool:
    expires_at = expiration_from_headers(headers)
    return expires_at > 0 and expires_at <= int(time.time() if now is None else now)


def expired_subscription_body(bot_username: str) -> bytes:
    username = bot_username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("invalid Telegram bot username")
    zero_uuid = "00000000-0000-0000-0000-000000000000"
    remarks = (
        "🔴 Подписка истекла",
        f"👉 Продли в боте @{username}",
    )
    links = [
        (
            f"vless://{zero_uuid}@127.0.0.1:{index}"
            "?encryption=none&type=tcp&security=none"
            f"#{quote(remark, safe='')}"
        )
        for index, remark in enumerate(remarks, start=1)
    ]
    return base64.b64encode(("\n".join(links) + "\n").encode("utf-8"))


def _headers_dict(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


class SubscriptionProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CEA-Subscription-Proxy"
    sys_version = ""

    def do_GET(self) -> None:
        if not SUBSCRIPTION_PATH_RE.fullmatch(self.path):
            self.send_error(404)
            return

        request = Request(
            f"{UPSTREAM_BASE_URL}{self.path}",
            headers={
                "Accept": "text/plain",
                "Accept-Encoding": "identity",
                "User-Agent": "CEA-Subscription-Proxy/1.0",
            },
        )
        try:
            try:
                upstream = urlopen(request, timeout=10)
            except HTTPError as exc:
                upstream = exc
            with upstream:
                status = int(upstream.status)
                headers = _headers_dict(upstream.headers.items())
                body = upstream.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, URLError, TimeoutError):
            logging.exception("Marzban subscription request failed")
            self.send_error(502)
            return

        if len(body) > MAX_RESPONSE_BYTES:
            self.send_error(502)
            return
        if status == 200 and subscription_has_expired(headers):
            body = expired_subscription_body(
                os.getenv("VPN_BOT_USERNAME", "ceavpn_bot")
            )

        self.send_response(status)
        for name, value in headers.items():
            if name in FORWARDED_HEADERS:
                self.send_header(name, value)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Subscription paths contain bearer tokens and must never reach logs.
        logging.info("subscription request completed")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = ThreadingHTTPServer(
        (LISTEN_HOST, LISTEN_PORT),
        SubscriptionProxyHandler,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
