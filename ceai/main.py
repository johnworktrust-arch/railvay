from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import ClientSession, ClientTimeout, web

from ceai.config import Settings, load_settings
from ceai.database import Database
from ceai.health import start_health_server
from ceai.internal_api import (
    handle_provider_settings_request,
    handle_provider_status_request,
)
from ceai.payment_notifications import notify_payment_result
from ceai.public_offer import PUBLIC_OFFER_TEXT
from ceai.runtime_diagnostics import (
    record_webhook_request,
    snapshot as diagnostics_snapshot,
)
from ceai.seed import seed_reference_data
from ceai.services.app import AppServices, build_services
from ceai.services.exceptions import BusinessRuleError, NotFoundError
from ceai.services.platega import PlategaCallbackAuthenticationError
from ceai.services.vpn import VpnPaymentVerificationError
from ceai.bot.handlers import create_router
from ceai.vpn_bot.handlers import (
    create_vpn_router,
    happ_subscription_instructions,
    subscription_copy_button,
    subscription_open_button,
    subscription_v2box_button,
)
from ceai.vpn_worker_api import register_vpn_worker_routes


BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="profile", description="Профиль"),
]
BOT_DESCRIPTION = (
    "🔥 Cea AI — современные AI-инструменты в одном боте.\n\n"
    "— общение с DeepSeek V4 Flash и ChatGPT GPT-5.6\n"
    "— создание и редактирование изображений в GPT Image 2\n"
    "— генерация видео в Kling 3.0\n"
    "— озвучка текста реалистичными голосами\n"
    "— оплата Telegram Stars, картой или СБП\n\n"
    "🚀 Нажмите /start, чтобы начать."
)
BOT_SHORT_DESCRIPTION = (
    "🚀 Все современные нейросети в одном боте. "
    "ℹ️ Канал @ceafamily"
)
PLATEGA_CALLBACK_MAX_BODY_BYTES = 64 * 1024
TELEGRAM_BOT_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def public_offer(request: web.Request) -> web.Response:
    return web.Response(text=PUBLIC_OFFER_TEXT, content_type="text/plain")


async def payment_return(request: web.Request) -> web.Response:
    bot_url = "https://t.me/aiceabot"
    return web.Response(
        text=(
            "<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"refresh\" content=\"0; url={bot_url}\">"
            "<title>Возвращаем в CeaAI</title>"
            f"<script>window.location.replace(\"{bot_url}\");</script>"
            "<body style=\"font-family: system-ui, sans-serif; padding: 32px;\">"
            "<h1>Возвращаем в Telegram</h1>"
            "<p>Платёж обрабатывается. Коины начислятся автоматически "
            "после подтверждения оплаты.</p>"
            "<p>Если Telegram не открылся автоматически, нажмите кнопку ниже.</p>"
            f"<p><a href=\"{bot_url}\" "
            "style=\"display:inline-block;padding:12px 18px;border-radius:10px;"
            "background:#2481cc;color:white;text-decoration:none;font-weight:600;\">"
            "Открыть CeaAI в Telegram</a></p>"
            "</body></html>"
        ),
        content_type="text/html",
    )


async def auto_renewal_loop(services: AppServices) -> None:
    interval_seconds = int(os.getenv("AUTO_RENEWAL_INTERVAL_SECONDS", "900"))
    while True:
        if services.settings.payment_provider != "yookassa":
            await asyncio.sleep(max(60, interval_seconds))
            continue
        try:
            results = await asyncio.to_thread(
                services.payments.process_due_auto_renewals
            )
            if results:
                logging.info("Processed %s YooKassa auto renewals", len(results))
        except Exception:
            logging.exception("YooKassa auto renewal loop failed")
        await asyncio.sleep(max(60, interval_seconds))


async def vpn_maintenance_loop(services: AppServices) -> None:
    interval_seconds = int(os.getenv("VPN_MAINTENANCE_INTERVAL_SECONDS", "60"))
    while True:
        try:
            reconciled = await asyncio.to_thread(
                services.vpn.reconcile_platega_payments
            )
            if reconciled:
                logging.info(
                    "Reconciled %s Platega VPN payment(s)", reconciled
                )
        except Exception:
            logging.exception("Platega VPN reconciliation loop failed")
        try:
            queued = await asyncio.to_thread(services.vpn.enqueue_due_expirations)
            if queued:
                logging.info("Queued %s expired VPN subscription(s)", queued)
        except Exception:
            logging.exception("VPN maintenance loop failed")
        await asyncio.sleep(max(30, interval_seconds))


def _normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _vpn_bot_url(settings: Settings) -> str:
    username = settings.vpn_bot_username.strip().lstrip("@")
    if not TELEGRAM_BOT_USERNAME_RE.fullmatch(username):
        username = "ceavpn_bot"
    return f"https://t.me/{username}"


async def vpn_payment_return(request: web.Request) -> web.Response:
    # This endpoint only takes the browser back to Telegram. Payment
    # fulfillment is intentionally restricted to the authenticated callback
    # or an explicit server-side status check.
    raise web.HTTPFound(location=_vpn_bot_url(request.app["settings"]))


async def vpn_payment_failed(request: web.Request) -> web.Response:
    # A failed browser redirect is not authoritative payment state either.
    raise web.HTTPFound(location=_vpn_bot_url(request.app["settings"]))


async def platega_payment_failed(request: web.Request) -> web.Response:
    raise web.HTTPFound(location="https://t.me/aiceabot")


def register_platega_routes(
    app: web.Application,
    *,
    settings: Settings,
    services: AppServices,
    bot: Bot,
) -> None:
    webhook_path = _normalize_path(settings.platega_webhook_path)
    return_path = _normalize_path(settings.platega_return_path)
    failed_path = _normalize_path(settings.platega_failed_path)

    async def platega_webhook(request: web.Request) -> web.Response:
        content_length = request.content_length
        if content_length is not None and content_length > PLATEGA_CALLBACK_MAX_BODY_BYTES:
            return web.json_response(
                {"ok": False, "error": "payload_too_large"}, status=413
            )
        raw_body = await request.read()
        if len(raw_body) > PLATEGA_CALLBACK_MAX_BODY_BYTES:
            return web.json_response(
                {"ok": False, "error": "payload_too_large"}, status=413
            )
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response(
                {"ok": False, "error": "invalid_json"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "invalid_payload"}, status=400
            )
        try:
            result = await asyncio.to_thread(
                services.payments.process_platega_webhook,
                headers=request.headers,
                payload=payload,
            )
        except PlategaCallbackAuthenticationError:
            return web.json_response(
                {"ok": False, "error": "invalid_authentication"}, status=401
            )
        except (BusinessRuleError, NotFoundError) as exc:
            logging.warning("Platega webhook rejected: %s", exc)
            return web.json_response(
                {"ok": False, "error": "invalid_callback"}, status=400
            )
        except Exception:
            logging.exception("Platega webhook failed")
            return web.json_response(
                {"ok": False, "error": "callback_unavailable"},
                status=503,
                headers={"Retry-After": "300"},
            )
        await notify_payment_result(bot=bot, services=services, result=result)
        return web.json_response(
            {
                "ok": True,
                "processed": result.processed,
                "duplicate": result.duplicate,
                "credited_coins": result.credited_coins,
            }
        )

    app.router.add_post(webhook_path, platega_webhook)
    app.router.add_get(return_path, payment_return)
    app.router.add_get(failed_path, platega_payment_failed)


def _secure_header_matches(expected: str, received: str) -> bool:
    return hmac.compare_digest(
        expected.encode("utf-8"),
        received.encode("utf-8"),
    )


def register_vpn_platega_routes(
    app: web.Application,
    *,
    settings: Settings,
    services: AppServices,
) -> None:
    webhook_path = _normalize_path(settings.vpn_platega_webhook_path)
    return_path = _normalize_path(settings.vpn_platega_return_path)
    failed_path = _normalize_path(settings.vpn_platega_failed_path)

    async def vpn_platega_webhook(request: web.Request) -> web.Response:
        content_length = request.content_length
        if (
            content_length is not None
            and content_length > PLATEGA_CALLBACK_MAX_BODY_BYTES
        ):
            return web.json_response(
                {"ok": False, "error": "payload_too_large"}, status=413
            )

        merchant_id = settings.vpn_platega_merchant_id.strip()
        secret = settings.vpn_platega_secret
        if not merchant_id or not secret:
            return web.json_response(
                {"ok": False, "error": "payment_provider_unavailable"},
                status=503,
                headers={"Retry-After": "300"},
            )

        merchant_headers = request.headers.getall("X-MerchantId", [])
        secret_headers = request.headers.getall("X-Secret", [])
        merchant_matches = (
            len(merchant_headers) == 1
            and _secure_header_matches(merchant_id, merchant_headers[0])
        )
        secret_matches = (
            len(secret_headers) == 1
            and _secure_header_matches(secret, secret_headers[0])
        )
        if not merchant_matches or not secret_matches:
            return web.json_response(
                {"ok": False, "error": "invalid_authentication"}, status=401
            )

        raw_body = await request.read()
        if len(raw_body) > PLATEGA_CALLBACK_MAX_BODY_BYTES:
            return web.json_response(
                {"ok": False, "error": "payload_too_large"}, status=413
            )
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response(
                {"ok": False, "error": "invalid_json"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "invalid_payload"}, status=400
            )

        try:
            await asyncio.to_thread(
                services.vpn.handle_platega_callback,
                headers=request.headers,
                payload=payload,
            )
        except VpnPaymentVerificationError:
            logging.warning("Platega VPN callback rejected")
            return web.json_response(
                {"ok": False, "error": "invalid_callback"}, status=400
            )
        except BusinessRuleError:
            # Fulfilment can fail for a temporary business dependency (for
            # example, no active VPN server). A 5xx keeps Platega retries alive
            # instead of permanently acknowledging an unfulfilled payment.
            logging.warning("Platega VPN callback temporarily unavailable")
            return web.json_response(
                {"ok": False, "error": "callback_unavailable"},
                status=503,
                headers={"Retry-After": "300"},
            )
        except Exception:
            # A 5xx response tells Platega to retry transient API/database
            # failures. Never include credentials or provider payloads here.
            logging.exception("Platega VPN callback failed")
            return web.json_response(
                {"ok": False, "error": "callback_unavailable"},
                status=503,
                headers={"Retry-After": "300"},
            )
        return web.json_response({"ok": True})

    app.router.add_post(webhook_path, vpn_platega_webhook)
    app.router.add_get(return_path, vpn_payment_return)
    app.router.add_get(failed_path, vpn_payment_failed)


def _crypto_webhook_path(settings: Settings) -> str:
    path = _normalize_path(settings.crypto_pay_webhook_path)
    secret = settings.crypto_pay_webhook_secret.strip().strip("/")
    if secret and not path.rstrip("/").endswith(f"/{secret}"):
        path = f"{path.rstrip('/')}/{secret}"
    return path


async def telegram_status(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    timeout = ClientTimeout(total=6)

    async def telegram_call(method: str) -> dict:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
        async with ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                payload = await response.json(content_type=None)
                return payload if isinstance(payload, dict) else {}

    try:
        me = await telegram_call("getMe")
        webhook = await telegram_call("getWebhookInfo")
    except Exception as exc:  # pragma: no cover - diagnostic endpoint
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    me_result = me.get("result") if isinstance(me.get("result"), dict) else {}
    webhook_result = (
        webhook.get("result") if isinstance(webhook.get("result"), dict) else {}
    )
    return web.json_response(
        {
            "ok": bool(me.get("ok")) and bool(webhook.get("ok")),
            "bot": {
                "id": me_result.get("id"),
                "username": me_result.get("username"),
                "first_name": me_result.get("first_name"),
            },
            "webhook": {
                "url": webhook_result.get("url"),
                "pending_update_count": webhook_result.get("pending_update_count"),
                "last_error_date": webhook_result.get("last_error_date"),
                "last_error_message": webhook_result.get("last_error_message"),
                "allowed_updates": webhook_result.get("allowed_updates"),
            },
            "diagnostics": diagnostics_snapshot(),
        }
    )


async def run_webhook(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    settings,
    db: Database,
    services: AppServices,
    webhook_url: str,
    webhook_path: str,
    webhook_secret: str,
    vpn_bot: Bot | None = None,
    vpn_dispatcher: Dispatcher | None = None,
    vpn_webhook_url: str = "",
    vpn_webhook_path: str = "",
    vpn_webhook_secret: str = "",
) -> None:
    @web.middleware
    async def diagnostics_middleware(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        if request.path == webhook_path and request.method == "POST":
            body = await request.read()
            record_webhook_request(
                path=request.path,
                method=request.method,
                body=body,
            )
        return await handler(request)

    app = web.Application(middlewares=[diagnostics_middleware])
    app["settings"] = settings
    app.router.add_get("/healthz", health)
    app.router.add_get("/public-offer", public_offer)
    yookassa_return_path = _normalize_path(settings.yookassa_return_path)
    yookassa_webhook_path = _normalize_path(settings.yookassa_webhook_path)
    crypto_webhook_path = _crypto_webhook_path(settings)
    app.router.add_get(yookassa_return_path, payment_return)
    app.router.add_get("/telegram/status", telegram_status)
    register_platega_routes(app, settings=settings, services=services, bot=bot)
    register_vpn_platega_routes(app, settings=settings, services=services)

    async def notify_vpn_ready(completion) -> None:
        if vpn_bot is None or completion.operation == "disable":
            return
        subscription_url = str(completion.subscription.get("subscription_url") or "")
        if not subscription_url:
            return
        try:
            await vpn_bot.send_message(
                chat_id=completion.telegram_id,
                text=(
                    "✅ <b>VPN готов!</b>\n\n"
                    "Ссылка персональная — не передавайте её другим.\n\n"
                    f"{happ_subscription_instructions()}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            subscription_open_button(
                                subscription_url,
                                settings.vpn_subscription_base_url,
                            )
                        ],
                        [
                            subscription_v2box_button(
                                subscription_url,
                                settings.vpn_subscription_base_url,
                            )
                        ],
                        [
                            subscription_copy_button(subscription_url)
                        ],
                        [
                            InlineKeyboardButton(
                                text="👤 Моя подписка",
                                callback_data="vpn:subscription",
                            )
                        ],
                    ]
                ),
            )
        except Exception:
            # The job is already committed; a Telegram delivery failure must not
            # make the worker repeat the provisioning result.
            logging.exception(
                "Could not notify Telegram user that VPN provisioning completed"
            )

    register_vpn_worker_routes(
        app,
        db=db,
        services=services,
        settings=settings,
        on_completed=notify_vpn_ready,
    )

    async def provider_settings(request: web.Request) -> web.Response:
        body = await request.read()
        status, content_type, response = await asyncio.to_thread(
            handle_provider_settings_request,
            settings=settings,
            db=db,
            headers=request.headers,
            body=body,
        )
        return web.Response(status=status, text=response, content_type=content_type)

    async def provider_status(request: web.Request) -> web.Response:
        status, content_type, response = await asyncio.to_thread(
            handle_provider_status_request,
            settings=settings,
            db=db,
            headers=request.headers,
        )
        return web.Response(status=status, text=response, content_type=content_type)

    app.router.add_post("/internal/provider-settings", provider_settings)
    app.router.add_get("/internal/provider-status", provider_status)

    async def yookassa_webhook(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "invalid_payload"}, status=400
            )
        try:
            result = await asyncio.to_thread(
                services.payments.process_yookassa_webhook,
                payload=payload,
            )
        except BusinessRuleError as exc:
            logging.warning("YooKassa webhook rejected: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            logging.exception("YooKassa webhook failed")
            return web.json_response(
                {"ok": False, "error": "webhook_failed"}, status=502
            )
        await notify_payment_result(bot=bot, services=services, result=result)
        return web.json_response(
            {
                "ok": True,
                "processed": result.processed,
                "duplicate": result.duplicate,
                "credited_coins": result.credited_coins,
            }
        )

    app.router.add_post(yookassa_webhook_path, yookassa_webhook)

    async def crypto_pay_webhook(request: web.Request) -> web.Response:
        raw_body = await request.read()
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "invalid_payload"}, status=400
            )
        try:
            result = await asyncio.to_thread(
                services.payments.process_crypto_pay_webhook,
                payload=payload,
                raw_body=raw_body,
                signature=request.headers.get("Crypto-Pay-API-Signature", ""),
            )
        except BusinessRuleError as exc:
            logging.warning("Crypto Pay webhook rejected: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            logging.exception("Crypto Pay webhook failed")
            return web.json_response(
                {"ok": False, "error": "webhook_failed"}, status=502
            )
        return web.json_response(
            {
                "ok": True,
                "processed": result.processed,
                "duplicate": result.duplicate,
                "credited_coins": result.credited_coins,
            }
        )

    app.router.add_post(crypto_webhook_path, crypto_pay_webhook)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=True,
        secret_token=webhook_secret or None,
    ).register(app, path=webhook_path)
    setup_application(app, dispatcher, bot=bot)

    if vpn_bot and vpn_dispatcher and vpn_webhook_path:
        SimpleRequestHandler(
            dispatcher=vpn_dispatcher,
            bot=vpn_bot,
            handle_in_background=True,
            secret_token=vpn_webhook_secret or None,
        ).register(app, path=vpn_webhook_path)
        setup_application(app, vpn_dispatcher, bot=vpn_bot)

    await bot.set_webhook(
        webhook_url,
        secret_token=webhook_secret or None,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    if vpn_bot and vpn_dispatcher and vpn_webhook_url:
        await vpn_bot.set_webhook(
            vpn_webhook_url,
            secret_token=vpn_webhook_secret or None,
            allowed_updates=vpn_dispatcher.resolve_used_update_types(),
        )

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    auto_renewal_task = asyncio.create_task(auto_renewal_loop(services))
    vpn_maintenance_task = asyncio.create_task(vpn_maintenance_loop(services))
    logging.info("Webhook endpoint listening on 0.0.0.0:%s%s", port, webhook_path)
    logging.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)
    try:
        await asyncio.Event().wait()
    finally:
        auto_renewal_task.cancel()
        vpn_maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await auto_renewal_task
        with suppress(asyncio.CancelledError):
            await vpn_maintenance_task
        await runner.cleanup()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required. Copy .env.example to .env.")
    _ensure_persistent_database(settings)

    db = Database(settings.database_url)
    db.migrate()
    seed_reference_data(db)

    services = build_services(db, settings)
    bot = Bot(token=settings.telegram_bot_token)
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_my_description(description=BOT_DESCRIPTION)
    await bot.set_my_description(description=BOT_DESCRIPTION, language_code="ru")
    await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
    await bot.set_my_short_description(
        short_description=BOT_SHORT_DESCRIPTION, language_code="ru"
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    vpn_bot = None
    vpn_dispatcher = None
    if settings.vpn_telegram_bot_token:
        vpn_bot = Bot(token=settings.vpn_telegram_bot_token)
        vpn_dispatcher = Dispatcher()
        vpn_dispatcher.include_router(create_vpn_router(services))
    health_server = None

    try:
        if settings.app_base_url:
            webhook_path = settings.telegram_webhook_path
            if not webhook_path.startswith("/"):
                webhook_path = "/" + webhook_path
            webhook_url = settings.app_base_url.rstrip("/") + webhook_path
            vpn_webhook_path = _normalize_path(settings.vpn_telegram_webhook_path)
            await run_webhook(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                db=db,
                services=services,
                webhook_url=webhook_url,
                webhook_path=webhook_path,
                webhook_secret=settings.telegram_webhook_secret,
                vpn_bot=vpn_bot,
                vpn_dispatcher=vpn_dispatcher,
                vpn_webhook_url=(
                    settings.app_base_url.rstrip("/") + vpn_webhook_path
                    if vpn_bot else ""
                ),
                vpn_webhook_path=vpn_webhook_path if vpn_bot else "",
                vpn_webhook_secret=settings.vpn_telegram_webhook_secret,
            )
        else:
            health_server = await start_health_server(settings=settings, db=db)
            await bot.delete_webhook(drop_pending_updates=False)
            auto_renewal_task = asyncio.create_task(auto_renewal_loop(services))
            vpn_maintenance_task = asyncio.create_task(vpn_maintenance_loop(services))
            try:
                if vpn_bot and vpn_dispatcher:
                    await vpn_bot.delete_webhook(drop_pending_updates=False)
                    await asyncio.gather(
                        dispatcher.start_polling(bot),
                        vpn_dispatcher.start_polling(vpn_bot),
                    )
                else:
                    await dispatcher.start_polling(bot)
            finally:
                auto_renewal_task.cancel()
                vpn_maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await auto_renewal_task
                with suppress(asyncio.CancelledError):
                    await vpn_maintenance_task
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        await bot.session.close()
        if vpn_bot is not None:
            await vpn_bot.session.close()
        db.close()


def _ensure_persistent_database(settings: Settings) -> None:
    managed_runtime = (
        settings.app_env.strip().lower() in {"prod", "production"}
        or bool(os.getenv("RAILWAY_ENVIRONMENT"))
        or bool(os.getenv("RAILWAY_PUBLIC_DOMAIN"))
        or bool(os.getenv("RAILWAY_SERVICE_ID"))
    )
    if (
        managed_runtime
        and settings.database_url.startswith("sqlite:///")
        and not settings.allow_ephemeral_sqlite
    ):
        raise SystemExit(
            "Refusing to start with SQLite in production/Railway. "
            "SQLite inside the deploy container can be recreated on deploy and users "
            "can lose subscriptions, payments, and coin balances. "
            "Attach Railway Postgres and set DATABASE_URL=postgresql://... "
            "Only set CEAI_ALLOW_EPHEMERAL_SQLITE=1 for disposable test bots."
        )


if __name__ == "__main__":
    asyncio.run(main())
