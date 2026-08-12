from __future__ import annotations

import asyncio
import logging
import os
from aiohttp import web

from ceavpn.config import Settings
from ceavpn.database import Database

HEALTH_HOST = "0.0.0.0"
DEFAULT_HEALTH_PORT = 8080


async def _handle_health_request(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def start_health_server(*, settings: Settings, db: Database) -> web.ServerRunner:
    app = web.Application()
    app["settings"] = settings
    app["db"] = db
    app.router.add_get("/healthz", _handle_health_request)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", str(DEFAULT_HEALTH_PORT)))
    site = web.TCPSite(runner, host=HEALTH_HOST, port=port)
    await site.start()
    logging.info("Health server started on %s:%s/healthz", HEALTH_HOST, port)
    return runner
