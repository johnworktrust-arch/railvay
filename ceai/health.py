from __future__ import annotations

import asyncio
import logging
import os
from typing import Mapping

from ceai.config import Settings
from ceai.database import Database
from ceai.internal_api import (
    handle_provider_settings_request,
    handle_provider_status_request,
)
from ceai.public_offer import PUBLIC_OFFER_TEXT


async def _handle_health_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    settings: Settings | None,
    db: Database | None,
) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    except Exception:
        raw = b""

    first_line = raw.splitlines()[0].decode("ascii", errors="ignore") if raw else ""
    parts = first_line.split()
    method = parts[0] if len(parts) >= 1 else "GET"
    path = parts[1] if len(parts) >= 2 else "/"
    request_headers = _parse_headers(raw)
    if path == "/healthz":
        status = "200 OK"
        content_type = "text/plain; charset=utf-8"
        body = b"ok\n"
    elif path == "/public-offer":
        status = "200 OK"
        content_type = "text/plain; charset=utf-8"
        body = PUBLIC_OFFER_TEXT.encode("utf-8")
    elif path == "/internal/provider-settings" and method == "POST":
        content_length = int(request_headers.get("content-length", "0") or "0")
        body_start = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
        if len(body_start) < content_length:
            body_start += await reader.readexactly(content_length - len(body_start))
        if settings is None or db is None:
            code, content_type, response = (
                503,
                "application/json",
                '{"ok": false, "error": "not_configured"}\n',
            )
        else:
            code, content_type, response = await asyncio.to_thread(
                handle_provider_settings_request,
                settings=settings,
                db=db,
                headers=request_headers,
                body=body_start,
            )
        status = _http_status(code)
        body = response.encode("utf-8")
    elif path == "/internal/provider-status" and method == "GET":
        if settings is None or db is None:
            code, content_type, response = (
                503,
                "application/json",
                '{"ok": false, "error": "not_configured"}\n',
            )
        else:
            code, content_type, response = await asyncio.to_thread(
                handle_provider_status_request,
                settings=settings,
                db=db,
                headers=request_headers,
            )
        status = _http_status(code)
        body = response.encode("utf-8")
    else:
        status = "404 Not Found"
        content_type = "text/plain; charset=utf-8"
        body = b"not found\n"

    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def start_health_server(
    settings: Settings | None = None, db: Database | None = None
) -> asyncio.AbstractServer | None:
    raw_port = os.getenv("PORT")
    if not raw_port:
        return None

    port = int(raw_port)
    server = await asyncio.start_server(
        lambda reader, writer: _handle_health_request(reader, writer, settings, db),
        host="0.0.0.0",
        port=port,
    )
    logging.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)
    return server


def _parse_headers(raw: bytes) -> Mapping[str, str]:
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n")[1:]:
        if not line:
            break
        try:
            key, value = line.decode("latin1").split(":", 1)
        except ValueError:
            continue
        headers[key.strip().lower()] = value.strip()
    return headers


def _http_status(code: int) -> str:
    reasons = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        503: "Service Unavailable",
    }
    return f"{code} {reasons.get(code, 'Unknown')}"
