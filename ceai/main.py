from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from ceai.config import load_settings
from ceai.database import Database
from ceai.health import start_health_server
from ceai.seed import seed_reference_data
from ceai.services.app import build_services
from ceai.bot.handlers import create_router


BOT_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="profile", description="Профиль"),
]


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def run_webhook(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    webhook_url: str,
    webhook_path: str,
    webhook_secret: str,
) -> None:
    app = web.Application()
    app.router.add_get("/healthz", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=webhook_secret or None,
    ).register(app, path=webhook_path)
    setup_application(app, dispatcher, bot=bot)

    await bot.set_webhook(
        webhook_url,
        secret_token=webhook_secret or None,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Webhook endpoint listening on 0.0.0.0:%s%s", port, webhook_path)
    logging.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required. Copy .env.example to .env.")

    db = Database(settings.database_url)
    db.migrate()
    seed_reference_data(db)

    services = build_services(db, settings)
    bot = Bot(token=settings.telegram_bot_token)
    await bot.set_my_commands(BOT_COMMANDS)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    health_server = None

    try:
        if settings.app_base_url:
            webhook_path = settings.telegram_webhook_path
            if not webhook_path.startswith("/"):
                webhook_path = "/" + webhook_path
            webhook_url = settings.app_base_url.rstrip("/") + webhook_path
            await run_webhook(
                bot=bot,
                dispatcher=dispatcher,
                webhook_url=webhook_url,
                webhook_path=webhook_path,
                webhook_secret=settings.telegram_webhook_secret,
            )
        else:
            health_server = await start_health_server()
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(bot)
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
