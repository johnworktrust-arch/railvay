from __future__ import annotations

import asyncio
import logging
import os


async def _handle_health_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    except Exception:
        raw = b""

    first_line = raw.splitlines()[0].decode("ascii", errors="ignore") if raw else ""
    parts = first_line.split()
    path = parts[1] if len(parts) >= 2 else "/"
    if path == "/healthz":
        status = "200 OK"
        body = b"ok\n"
    else:
        status = "404 Not Found"
        body = b"not found\n"

    headers = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def start_health_server() -> asyncio.AbstractServer | None:
    raw_port = os.getenv("PORT")
    if not raw_port:
        return None

    port = int(raw_port)
    server = await asyncio.start_server(
        _handle_health_request, host="0.0.0.0", port=port
    )
    logging.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)
    return server
