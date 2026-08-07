from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from aiohttp import web

from ceaadmin.config import Settings, load_settings
from ceaadmin.database import Database
from ceaadmin.services.app import AppServices, build_services
from ceaadmin.services.exceptions import BusinessRuleError, NotFoundError
from ceaadmin.services.vpn_admin import VpnAdminService


ASSETS_DIR = Path(__file__).resolve().parent / "admin_assets"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_REQUEST_BYTES = 8 * 1024
AUTH_COOKIE_NAME = "cea_admin_session"
AUTH_SESSION_SECONDS = 7 * 24 * 60 * 60
PUBLIC_PATHS = {"/healthz", "/login", "/assets/login.css"}
DB_KEY = web.AppKey("db", Database)
SERVICES_KEY = web.AppKey("services", AppServices)
SETTINGS_KEY = web.AppKey("settings", Settings)
ADMIN_TOKEN_KEY = web.AppKey("admin_token", str)
OPERATOR_KEY = web.AppKey("operator", object)
DATABASE_LABEL_KEY = web.AppKey("database_label", str)
VPN_ADMIN_KEY = web.AppKey("vpn_admin", VpnAdminService)
LOGIN_REQUIRED_KEY = web.AppKey("login_required", bool)
SECURE_COOKIES_KEY = web.AppKey("secure_cookies", bool)


def _json_response(payload: Any, *, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
    )


def _redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


def _database_label(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        return "Локальная SQLite"
    parsed = urlparse(database_url)
    host = parsed.hostname or "PostgreSQL"
    return f"PostgreSQL · {host}"


def _session_token(secret: str, *, now: int | None = None) -> str:
    expires_at = (now if now is not None else int(time.time())) + AUTH_SESSION_SECONDS
    payload = f"{expires_at}.{secrets.token_urlsafe(18)}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _valid_session(token: str, secret: str, *, now: int | None = None) -> bool:
    try:
        payload, supplied_signature = token.rsplit(".", 1)
        expires_text, _nonce = payload.split(".", 1)
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return False

    current_time = now if now is not None else int(time.time())
    if expires_at < current_time:
        return False
    if expires_at > current_time + AUTH_SESSION_SECONDS + 60:
        return False

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def _is_authenticated(request: web.Request) -> bool:
    if not request.app[LOGIN_REQUIRED_KEY]:
        return True
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    return _valid_session(
        token,
        request.app[SETTINGS_KEY].admin_web_session_secret,
    )


def _find_operator(
    services: AppServices, settings: Settings
) -> Dict[str, Any] | None:
    for telegram_id in settings.admin_telegram_ids:
        user = services.users.get_by_telegram_id(telegram_id)
        if user:
            admin = services.admin.ensure_admin_access(user)
            if admin and services.admin.can_manage(admin):
                return admin

    for username in settings.admin_telegram_usernames:
        user = services.admin.find_user(username)
        if user:
            admin = services.admin.ensure_admin_access(user)
            if admin and services.admin.can_manage(admin):
                return admin

    with services.admin.db.transaction() as conn:
        row = conn.execute(
            """
            SELECT au.*, u.telegram_id, u.username
            FROM admin_users au
            JOIN users u ON u.id = au.user_id
            WHERE au.is_active = TRUE
              AND au.role IN ('owner', 'admin')
            ORDER BY CASE WHEN au.role = 'owner' THEN 0 ELSE 1 END, au.id
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


async def _read_json(request: web.Request) -> Dict[str, Any]:
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_REQUEST_BYTES, actual_size=request.content_length
        )
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Некорректный JSON")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Ожидается JSON-объект")
    return payload


def _require_operator(request: web.Request) -> Dict[str, Any]:
    operator = request.app[OPERATOR_KEY]
    if not operator:
        raise web.HTTPForbidden(
            text="Управление недоступно: администратор не найден в этой базе."
        )
    return operator


@web.middleware
async def security_headers_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "script-src 'self'; "
        "style-src 'self'",
    )
    if request.app[SECURE_COOKIES_KEY]:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@web.middleware
async def admin_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    if (
        request.app[LOGIN_REQUIRED_KEY]
        and request.path not in PUBLIC_PATHS
        and not _is_authenticated(request)
    ):
        if request.path.startswith("/api/"):
            return _json_response({"error": "authentication required"}, status=401)
        return _redirect("/login")

    if request.path.startswith("/api/"):
        supplied = request.headers.get("X-Cea-Admin-Token", "")
        expected = request.app[ADMIN_TOKEN_KEY]
        if not supplied or not hmac.compare_digest(supplied, expected):
            return _json_response({"error": "unauthorized"}, status=401)
    try:
        try:
            return await handler(request)
        except Exception as exc:
            db = request.app[DB_KEY]
            if (
                request.method == "GET"
                and request.path.startswith("/api/")
                and db.is_connection_error(exc)
            ):
                logging.warning(
                    "Postgres connection dropped; reconnecting admin dashboard"
                )
                await asyncio.to_thread(db.reconnect)
                return await handler(request)
            raise
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            return _json_response(
                {"error": exc.text or exc.reason}, status=exc.status
            )
        raise
    except NotFoundError as exc:
        return _json_response({"error": str(exc)}, status=404)
    except (BusinessRuleError, ValueError) as exc:
        return _json_response({"error": str(exc)}, status=400)
    except Exception:
        logging.exception("Admin dashboard request failed")
        return _json_response({"error": "Внутренняя ошибка сервера"}, status=500)


def create_admin_app(
    *,
    db: Database,
    settings: Settings,
    vpn_db: Database | None = None,
    admin_token: str | None = None,
    require_login: bool = False,
    secure_cookies: bool = False,
) -> web.Application:
    if require_login:
        if len(settings.admin_web_password) < 20:
            raise ValueError("ADMIN_WEB_PASSWORD must contain at least 20 characters")
        if len(settings.admin_web_session_secret) < 32:
            raise ValueError(
                "ADMIN_WEB_SESSION_SECRET must contain at least 32 characters"
            )

    target_vpn_db = vpn_db or db
    services = build_services(db, settings, vpn_db=target_vpn_db)
    token = admin_token or secrets.token_urlsafe(32)
    app = web.Application(
        middlewares=[security_headers_middleware, admin_middleware],
        client_max_size=MAX_REQUEST_BYTES,
    )
    app[DB_KEY] = db
    app[SERVICES_KEY] = services
    app[SETTINGS_KEY] = settings
    app[ADMIN_TOKEN_KEY] = token
    app[OPERATOR_KEY] = _find_operator(services, settings)
    app[VPN_ADMIN_KEY] = VpnAdminService(target_vpn_db, settings)
    app[LOGIN_REQUIRED_KEY] = require_login
    app[SECURE_COOKIES_KEY] = secure_cookies
    database_url = settings.admin_database_url or settings.database_url
    app[DATABASE_LABEL_KEY] = _database_label(database_url)

    def login_page(*, error: str = "", status: int = 200) -> web.Response:
        template = (ASSETS_DIR / "login.html").read_text(encoding="utf-8")
        error_markup = (
            f'<p class="login-error" role="alert">{html.escape(error)}</p>'
            if error
            else ""
        )
        return web.Response(
            text=template.replace("__LOGIN_ERROR__", error_markup),
            status=status,
            content_type="text/html",
        )

    async def healthz(_request: web.Request) -> web.Response:
        return _json_response({"status": "ok"})

    async def login(request: web.Request) -> web.Response:
        if not app[LOGIN_REQUIRED_KEY] or _is_authenticated(request):
            return _redirect("/")
        if request.method == "GET":
            return login_page()

        form = await request.post()
        supplied = str(form.get("password", "")).encode("utf-8")
        expected = settings.admin_web_password.encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            await asyncio.sleep(0.35)
            return login_page(
                error="Неверный пароль. Проверьте ввод и попробуйте снова.",
                status=401,
            )

        response = _redirect("/")
        response.set_cookie(
            AUTH_COOKIE_NAME,
            _session_token(settings.admin_web_session_secret),
            max_age=AUTH_SESSION_SECONDS,
            httponly=True,
            secure=app[SECURE_COOKIES_KEY],
            samesite="Strict",
            path="/",
        )
        return response

    async def logout(_request: web.Request) -> web.Response:
        response = _redirect("/login")
        response.del_cookie(
            AUTH_COOKIE_NAME,
            path="/",
            secure=app[SECURE_COOKIES_KEY],
            httponly=True,
            samesite="Strict",
        )
        return response

    async def index(_request: web.Request) -> web.Response:
        template = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        rendered = template.replace(
            "__ADMIN_TOKEN__", html.escape(token, quote=True)
        ).replace(
            "__DATABASE_LABEL__",
            html.escape(app[DATABASE_LABEL_KEY], quote=True),
        ).replace(
            "__REMOTE_CONTROLS__",
            (
                '<form action="/logout" method="post">'
                '<button class="logout-button" type="submit">Выйти</button>'
                "</form>"
                if app[LOGIN_REQUIRED_KEY]
                else ""
            ),
        )
        return web.Response(
            text=rendered,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in {"app.css", "app.js", "login.css"}:
            raise web.HTTPNotFound()
        content_type = "text/css" if name.endswith(".css") else "text/javascript"
        return web.Response(
            body=(ASSETS_DIR / name).read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    async def status(_request: web.Request) -> web.Response:
        return _json_response(
            {
                "database": app[DATABASE_LABEL_KEY],
                "can_manage": bool(app[OPERATOR_KEY]),
                "maintenance_active": await asyncio.to_thread(
                    services.admin.is_maintenance_mode_active
                ),
            }
        )

    async def stats(_request: web.Request) -> web.Response:
        data = await asyncio.to_thread(services.admin.dashboard_stats)
        total = int(data.get("users_total") or 0)
        paid = int(data.get("paid_users") or 0)
        data["conversion_percent"] = round(paid * 100 / total, 1) if total else 0
        return _json_response(data)

    async def users(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
            page_size = int(request.query.get("page_size", "25"))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректный номер страницы")
        data = await asyncio.to_thread(
            services.admin.list_web_users,
            page=page,
            page_size=page_size,
            query=request.query.get("q", ""),
            segment=request.query.get("segment", "all"),
        )
        return _json_response(data)

    async def vpn_stats(_request: web.Request) -> web.Response:
        data = await asyncio.to_thread(app[VPN_ADMIN_KEY].dashboard)
        return _json_response(data)

    async def vpn_users(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
            page_size = int(request.query.get("page_size", "25"))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректный номер страницы")
        data = await asyncio.to_thread(
            app[VPN_ADMIN_KEY].list_users,
            page=page,
            page_size=page_size,
            query=request.query.get("q", ""),
            segment=request.query.get("segment", "all"),
        )
        return _json_response(data)

    async def vpn_user_card(request: web.Request) -> web.Response:
        user_id = int(request.match_info["user_id"])
        card = await asyncio.to_thread(
            app[VPN_ADMIN_KEY].user_card,
            user_id,
        )
        return _json_response(card)

    async def set_vpn_abuse_blocked(request: web.Request) -> web.Response:
        operator = _require_operator(request)
        user_id = int(request.match_info["user_id"])
        payload = await _read_json(request)
        blocked = payload.get("blocked")
        if not isinstance(blocked, bool):
            raise web.HTTPBadRequest(text="Поле blocked должно быть boolean")
        reason = str(payload.get("reason") or "").strip()
        card = await asyncio.to_thread(
            app[VPN_ADMIN_KEY].set_abuse_blocked,
            user_id=user_id,
            is_blocked=blocked,
            reason=reason,
            admin_user_id=int(operator["user_id"]),
        )
        return _json_response({"ok": True, "user": card})

    async def user_card(request: web.Request) -> web.Response:
        user_id = int(request.match_info["user_id"])
        card = await asyncio.to_thread(services.admin.user_card, user_id)
        return _json_response(card)

    async def set_blocked(request: web.Request) -> web.Response:
        operator = _require_operator(request)
        user_id = int(request.match_info["user_id"])
        payload = await _read_json(request)
        blocked = payload.get("blocked")
        if not isinstance(blocked, bool):
            raise web.HTTPBadRequest(text="Поле blocked должно быть boolean")
        await asyncio.to_thread(
            services.admin.set_blocked,
            admin=operator,
            target_user_id=user_id,
            is_blocked=blocked,
        )
        return _json_response(
            {
                "ok": True,
                "user": await asyncio.to_thread(services.admin.user_card, user_id),
            }
        )

    async def credit(request: web.Request) -> web.Response:
        operator = _require_operator(request)
        user_id = int(request.match_info["user_id"])
        payload = await _read_json(request)
        try:
            amount = int(payload.get("amount"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Введите целое число коинов")
        if amount > 100_000:
            raise web.HTTPBadRequest(text="За один раз можно начислить до 100 000 коинов")
        balance = await asyncio.to_thread(
            services.admin.manual_credit,
            admin=operator,
            target_user_id=user_id,
            amount=amount,
        )
        return _json_response(
            {
                "ok": True,
                "balance": balance,
                "user": await asyncio.to_thread(services.admin.user_card, user_id),
            }
        )

    async def maintenance(request: web.Request) -> web.Response:
        operator = _require_operator(request)
        payload = await _read_json(request)
        active = payload.get("active")
        if not isinstance(active, bool):
            raise web.HTTPBadRequest(text="Поле active должно быть boolean")
        result = await asyncio.to_thread(
            services.admin.set_maintenance_mode,
            admin=operator,
            is_active=active,
        )
        return _json_response({"ok": True, "maintenance_active": result})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/login", login)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_get("/", index)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/users", users)
    app.router.add_get("/api/users/{user_id:\\d+}", user_card)
    app.router.add_get("/api/vpn/stats", vpn_stats)
    app.router.add_get("/api/vpn/users", vpn_users)
    app.router.add_get("/api/vpn/users/{user_id:\\d+}", vpn_user_card)
    app.router.add_post(
        "/api/vpn/users/{user_id:\\d+}/abuse-blocked",
        set_vpn_abuse_blocked,
    )
    app.router.add_post("/api/users/{user_id:\\d+}/blocked", set_blocked)
    app.router.add_post("/api/users/{user_id:\\d+}/credit", credit)
    app.router.add_post("/api/maintenance", maintenance)
    return app


async def run_admin_web(*, host: str, port: int) -> None:
    settings = load_settings()
    database_url = settings.admin_database_url or settings.database_url
    remote_access = host not in LOCAL_HOSTS
    if remote_access:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise SystemExit("Remote admin dashboard requires PostgreSQL.")
        if len(settings.admin_web_password) < 20:
            raise SystemExit(
                "Remote admin dashboard requires ADMIN_WEB_PASSWORD "
                "with at least 20 characters."
            )
        if len(settings.admin_web_session_secret) < 32:
            raise SystemExit(
                "Remote admin dashboard requires ADMIN_WEB_SESSION_SECRET "
                "with at least 32 characters."
            )

    db = Database(database_url)
    db.migrate()

    vpn_db = db
    if settings.vpn_database_url and settings.vpn_database_url != database_url:
        vpn_db = Database(settings.vpn_database_url)
        vpn_db.migrate()

    app = create_admin_app(
        db=db,
        vpn_db=vpn_db,
        settings=settings,
        require_login=remote_access,
        secure_cookies=remote_access,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    print(f"Cea AI Admin: http://{host}:{port}")
    print(f"Источник данных: {_database_label(database_url)}")
    print(f"Защита паролем: {'включена' if remote_access else 'локальный режим'}")
    if not app[OPERATOR_KEY]:
        print("Режим только для чтения: администратор не найден в выбранной базе.")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        db.close()


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Админка Cea AI")
    parser.add_argument("--host", default=settings.admin_web_host)
    parser.add_argument("--port", type=int, default=settings.admin_web_port)
    args = parser.parse_args()
    asyncio.run(run_admin_web(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
