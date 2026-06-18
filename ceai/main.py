from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from ceai.config import load_settings
from ceai.database import Database
from ceai.health import start_health_server
from ceai.seed import seed_reference_data
from ceai.services.app import build_services
from ceai.bot.handlers import create_router


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
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    health_server = await start_health_server()

    try:
        await dispatcher.start_polling(bot)
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
