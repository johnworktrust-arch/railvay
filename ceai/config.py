from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PUBLIC_OFFER_URL = (
    "https://telegra.ph/Polzovatelskoe-soglashenie-Cea-AI-07-16"
)
DEFAULT_PRIVACY_POLICY_URL = (
    "https://telegra.ph/Politika-konfidencialnosti-Cea-AI-07-16"
)
DEFAULT_INFO_CHANNEL_URL = "https://t.me/ceafamily"
KLING_API_KEY_NAMES = (
    "KLING_API_KEY",
    "KLINGAI_API_KEY",
    "KLING_AI_API_KEY",
    "KLING_KEY",
    "KLING_API",
    "KLING_API_TOKEN",
    "KLING_TOKEN",
    "KLING_ACCESS_KEY",
    "KLING_SECRET_KEY",
    "API_KEY_KLING",
)
VPN_WORKER_ID_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")


def _load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _normalize_telegram_url(value: str) -> str:
    cleaned = value.strip()
    if cleaned in {"https://t.me/cea_family", "http://t.me/cea_family", "@cea_family"}:
        return DEFAULT_INFO_CHANNEL_URL
    if cleaned.startswith("@"):
        return f"https://t.me/{cleaned[1:]}"
    return cleaned


def _normalize_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    return f"https://{cleaned}"


@dataclass(frozen=True)
class VpnAdditionalServer:
    code: str
    name: str
    region: str
    worker_id: str
    subscription_base_url: str
    is_active: bool


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_url: str
    app_env: str
    mock_payment_base_url: str
    vpn_telegram_bot_token: str = ""
    payment_provider: str = "mock"
    app_base_url: str = ""
    telegram_webhook_path: str = "/telegram/webhook"
    telegram_webhook_secret: str = ""
    vpn_telegram_webhook_path: str = "/telegram/vpn/webhook"
    vpn_telegram_webhook_secret: str = ""
    vpn_bot_username: str = ""
    admin_telegram_ids: Tuple[int, ...] = ()
    admin_telegram_usernames: Tuple[str, ...] = ()
    admin_database_url: str = ""
    admin_web_host: str = "127.0.0.1"
    admin_web_port: int = 8090
    admin_web_password: str = ""
    admin_web_session_secret: str = ""
    public_offer_url: str = DEFAULT_PUBLIC_OFFER_URL
    privacy_policy_url: str = DEFAULT_PRIVACY_POLICY_URL
    info_channel_url: str = DEFAULT_INFO_CHANNEL_URL
    support_username: str = "cea_help"
    vpn_support_username: str = "cea_help"
    vpn_channel_url: str = DEFAULT_INFO_CHANNEL_URL
    vpn_server_code: str = "nl-1"
    vpn_worker_id: str = "cea-vpn-nl1"
    vpn_worker_secret: str = ""
    vpn_worker_secrets: Tuple[Tuple[str, str], ...] = ()
    vpn_additional_servers: Tuple[VpnAdditionalServer, ...] = ()
    vpn_subscription_base_url: str = ""
    vpn_delivery_base_url: str = ""
    vpn_delivery_signing_secret: str = ""
    vpn_extra_profiles_json: str = "[]"
    vpn_trial_days: int = 3
    vpn_allow_admin_demo_payment: bool = False
    vpn_admin_demo_telegram_ids: Tuple[int, ...] = ()
    vpn_payment_provider: str = "disabled"
    vpn_platega_merchant_id: str = ""
    vpn_platega_secret: str = ""
    vpn_platega_api_base_url: str = "https://app.platega.io"
    vpn_platega_webhook_path: str = "/payments/vpn/platega/webhook"
    vpn_platega_return_path: str = "/payments/vpn/platega/return"
    vpn_platega_failed_path: str = "/payments/vpn/platega/failed"
    vpn_platega_request_timeout_seconds: int = 30
    vpn_worker_clock_skew_seconds: int = 300
    vpn_worker_lease_seconds: int = 120
    vpn_worker_health_max_age_seconds: int = 120
    ai_provider_mode: str = "auto"
    ai_request_timeout_seconds: int = 60
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    openai_api_key: str = ""
    openai_image_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    kling_api_key: str = ""
    kling_base_url: str = "https://api-singapore.klingai.com"
    kling_poll_interval_seconds: int = 10
    kling_poll_timeout_seconds: int = 600
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_api_base_url: str = "https://api.yookassa.ru/v3"
    yookassa_webhook_path: str = "/payments/yookassa/webhook"
    yookassa_return_path: str = "/payments/yookassa/return"
    yookassa_request_timeout_seconds: int = 15
    platega_merchant_id: str = ""
    platega_secret: str = ""
    platega_api_base_url: str = "https://app.platega.io"
    platega_webhook_path: str = "/payments/platega/webhook"
    platega_return_path: str = "/payments/platega/return"
    platega_failed_path: str = "/payments/platega/failed"
    platega_request_timeout_seconds: int = 30
    crypto_pay_token: str = ""
    crypto_pay_api_base_url: str = "https://testnet-pay.crypt.bot/api"
    crypto_pay_webhook_secret: str = ""
    crypto_pay_webhook_path: str = "/payments/crypto/webhook"
    crypto_pay_accepted_assets: str = "USDT"
    crypto_pay_request_timeout_seconds: int = 15
    telegram_stars_amount: int = 0
    allow_ephemeral_sqlite: bool = False


def load_settings() -> Settings:
    dotenv_values = _load_dotenv(BASE_DIR / ".env")

    def read(name: str, default: str = "") -> str:
        return os.getenv(name) or dotenv_values.get(name, default)

    def read_any(names: Tuple[str, ...], default: str = "") -> str:
        for name in names:
            value = read(name)
            if value.strip():
                return value.strip()

        normalized_names = {name.strip().upper().rstrip(";") for name in names}
        for source in (os.environ, dotenv_values):
            for key, value in source.items():
                normalized_key = key.strip().upper().rstrip(";")
                if normalized_key in normalized_names and value.strip():
                    return value.strip().strip('"').strip("'")
        return default

    def read_int_list(name: str) -> Tuple[int, ...]:
        values: list[int] = []
        for item in read(name).split(","):
            item = item.strip()
            if item:
                values.append(int(item))
        return tuple(values)

    def read_username_list(name: str) -> Tuple[str, ...]:
        values: list[str] = []
        for item in read(name).split(","):
            username = item.strip().lstrip("@").lower()
            if username:
                values.append(username)
        return tuple(values)

    def read_int(name: str, default: int) -> int:
        raw = read(name, str(default)).strip()
        return int(raw) if raw else default

    def read_bool(name: str, default: bool = False) -> bool:
        raw = read(name, "1" if default else "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def read_worker_secrets() -> Tuple[Tuple[str, str], ...]:
        raw = read("VPN_WORKER_SECRETS_JSON").strip()
        if not raw:
            return ()
        duplicate_key = False

        def reject_duplicate_keys(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            nonlocal duplicate_key
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    duplicate_key = True
                result[key] = value
            return result

        try:
            payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("VPN_WORKER_SECRETS_JSON must be valid JSON") from exc
        if duplicate_key or not isinstance(payload, dict):
            raise ValueError("VPN_WORKER_SECRETS_JSON must be a JSON object")
        values: list[Tuple[str, str]] = []
        seen_worker_ids: set[str] = set()
        for worker_id, secret in payload.items():
            if not isinstance(worker_id, str) or not isinstance(secret, str):
                raise ValueError(
                    "VPN_WORKER_SECRETS_JSON keys and values must be strings"
                )
            normalized_worker_id = worker_id.strip()
            if (
                VPN_WORKER_ID_RE.fullmatch(normalized_worker_id) is None
                or normalized_worker_id in seen_worker_ids
                or len(secret.encode("utf-8")) < 32
            ):
                raise ValueError("Invalid per-worker VPN secret")
            seen_worker_ids.add(normalized_worker_id)
            values.append((normalized_worker_id, secret))
        return tuple(sorted(values))

    def read_additional_vpn_servers() -> Tuple[VpnAdditionalServer, ...]:
        raw = read("VPN_ADDITIONAL_SERVERS_JSON", "[]").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "VPN_ADDITIONAL_SERVERS_JSON must be valid JSON"
            ) from exc
        if not isinstance(payload, list) or len(payload) > 8:
            raise ValueError(
                "VPN_ADDITIONAL_SERVERS_JSON must be a short JSON array"
            )

        allowed_keys = {
            "code",
            "name",
            "region",
            "worker_id",
            "subscription_base_url",
            "is_active",
        }
        servers: list[VpnAdditionalServer] = []
        seen_codes: set[str] = set()
        seen_workers: set[str] = set()
        for item in payload:
            if not isinstance(item, dict) or set(item) != allowed_keys:
                raise ValueError(
                    "VPN_ADDITIONAL_SERVERS_JSON contains invalid fields"
                )
            code = item["code"]
            name = item["name"]
            region = item["region"]
            worker_id = item["worker_id"]
            subscription_base_url = item["subscription_base_url"]
            is_active = item["is_active"]
            if (
                not isinstance(code, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", code) is None
                or not isinstance(name, str)
                or not name.strip()
                or len(name.strip()) > 80
                or not isinstance(region, str)
                or re.fullmatch(r"[A-Z]{2}", region.strip()) is None
                or not isinstance(worker_id, str)
                or VPN_WORKER_ID_RE.fullmatch(worker_id.strip()) is None
                or not isinstance(subscription_base_url, str)
                or not isinstance(is_active, bool)
            ):
                raise ValueError(
                    "VPN_ADDITIONAL_SERVERS_JSON contains invalid values"
                )

            normalized_url = subscription_base_url.strip().rstrip("/")
            url = urlsplit(normalized_url)
            try:
                url_port = url.port
            except ValueError as exc:
                raise ValueError(
                    "VPN additional subscription URL is invalid"
                ) from exc
            if (
                url.scheme != "https"
                or not url.hostname
                or url_port != 8443
                or url.username is not None
                or url.password is not None
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "VPN additional subscription URL must be HTTPS on port 8443"
                )

            normalized_code = code.strip()
            normalized_worker = worker_id.strip()
            if normalized_code in seen_codes or normalized_worker in seen_workers:
                raise ValueError(
                    "VPN_ADDITIONAL_SERVERS_JSON contains duplicate identity"
                )
            seen_codes.add(normalized_code)
            seen_workers.add(normalized_worker)
            servers.append(
                VpnAdditionalServer(
                    code=normalized_code,
                    name=name.strip(),
                    region=region.strip(),
                    worker_id=normalized_worker,
                    subscription_base_url=normalized_url,
                    is_active=is_active,
                )
            )
        return tuple(servers)

    app_base_url = _normalize_base_url(
        read("APP_BASE_URL") or read("RAILWAY_PUBLIC_DOMAIN")
    )
    public_offer_default = (
        f"{app_base_url}/public-offer" if app_base_url else DEFAULT_PUBLIC_OFFER_URL
    )
    privacy_policy_default = (
        f"{app_base_url}/privacy-policy"
        if app_base_url
        else DEFAULT_PRIVACY_POLICY_URL
    )

    return Settings(
        telegram_bot_token=read("TELEGRAM_BOT_TOKEN"),
        vpn_telegram_bot_token=read("VPN_TELEGRAM_BOT_TOKEN"),
        database_url=read("DATABASE_URL", "sqlite:///./data/ceai.sqlite3"),
        app_env=read("APP_ENV", "local"),
        mock_payment_base_url=read(
            "MOCK_PAYMENT_BASE_URL", "https://mock-payments.local/pay"
        ),
        payment_provider=read("PAYMENT_PROVIDER", "mock").strip().lower(),
        app_base_url=app_base_url,
        telegram_webhook_path=read("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook"),
        telegram_webhook_secret=read("TELEGRAM_WEBHOOK_SECRET"),
        vpn_telegram_webhook_path=read(
            "VPN_TELEGRAM_WEBHOOK_PATH", "/telegram/vpn/webhook"
        ),
        vpn_telegram_webhook_secret=read("VPN_TELEGRAM_WEBHOOK_SECRET"),
        vpn_bot_username=read("VPN_BOT_USERNAME").strip().lstrip("@"),
        admin_telegram_ids=read_int_list("ADMIN_TELEGRAM_IDS"),
        admin_telegram_usernames=read_username_list("ADMIN_TELEGRAM_USERNAMES"),
        admin_database_url=read("ADMIN_DATABASE_URL").strip(),
        admin_web_host=read("ADMIN_WEB_HOST", "127.0.0.1").strip(),
        admin_web_port=read_int("ADMIN_WEB_PORT", 8090),
        admin_web_password=read("ADMIN_WEB_PASSWORD"),
        admin_web_session_secret=read("ADMIN_WEB_SESSION_SECRET"),
        public_offer_url=read("PUBLIC_OFFER_URL", public_offer_default),
        privacy_policy_url=read("PRIVACY_POLICY_URL", privacy_policy_default),
        info_channel_url=_normalize_telegram_url(
            read("INFO_CHANNEL_URL", DEFAULT_INFO_CHANNEL_URL)
        ),
        support_username=read("SUPPORT_USERNAME", "cea_help").strip().lstrip("@"),
        vpn_support_username=read(
            "VPN_SUPPORT_USERNAME", read("SUPPORT_USERNAME", "cea_help")
        ).strip().lstrip("@"),
        vpn_channel_url=_normalize_telegram_url(
            read("VPN_CHANNEL_URL", read("INFO_CHANNEL_URL", DEFAULT_INFO_CHANNEL_URL))
        ),
        vpn_server_code=read("VPN_SERVER_CODE", "nl-1").strip(),
        vpn_worker_id=read("VPN_WORKER_ID", "cea-vpn-nl1").strip(),
        vpn_worker_secret=read("VPN_WORKER_SECRET"),
        vpn_worker_secrets=read_worker_secrets(),
        vpn_additional_servers=read_additional_vpn_servers(),
        vpn_subscription_base_url=_normalize_base_url(
            read("VPN_SUBSCRIPTION_BASE_URL")
        ),
        vpn_delivery_base_url=_normalize_base_url(
            read("VPN_DELIVERY_BASE_URL", app_base_url)
        ),
        vpn_delivery_signing_secret=read("VPN_DELIVERY_SIGNING_SECRET"),
        vpn_extra_profiles_json=read("VPN_EXTRA_PROFILES_JSON", "[]"),
        vpn_trial_days=read_int("VPN_TRIAL_DAYS", 3),
        vpn_allow_admin_demo_payment=read_bool(
            "VPN_ALLOW_ADMIN_DEMO_PAYMENT", False
        ),
        vpn_admin_demo_telegram_ids=read_int_list(
            "VPN_ADMIN_DEMO_TELEGRAM_IDS"
        ),
        vpn_payment_provider=read(
            "VPN_PAYMENT_PROVIDER", "disabled"
        ).strip().lower(),
        vpn_platega_merchant_id=read("VPN_PLATEGA_MERCHANT_ID").strip(),
        vpn_platega_secret=read("VPN_PLATEGA_SECRET"),
        vpn_platega_api_base_url=_normalize_base_url(
            read("VPN_PLATEGA_API_BASE_URL", "https://app.platega.io")
        ),
        vpn_platega_webhook_path=read(
            "VPN_PLATEGA_WEBHOOK_PATH", "/payments/vpn/platega/webhook"
        ),
        vpn_platega_return_path=read(
            "VPN_PLATEGA_RETURN_PATH", "/payments/vpn/platega/return"
        ),
        vpn_platega_failed_path=read(
            "VPN_PLATEGA_FAILED_PATH", "/payments/vpn/platega/failed"
        ),
        vpn_platega_request_timeout_seconds=read_int(
            "VPN_PLATEGA_REQUEST_TIMEOUT_SECONDS", 30
        ),
        vpn_worker_clock_skew_seconds=read_int(
            "VPN_WORKER_CLOCK_SKEW_SECONDS", 300
        ),
        vpn_worker_lease_seconds=read_int("VPN_WORKER_LEASE_SECONDS", 120),
        vpn_worker_health_max_age_seconds=read_int(
            "VPN_WORKER_HEALTH_MAX_AGE_SECONDS", 120
        ),
        ai_provider_mode=read("AI_PROVIDER_MODE", "auto").strip().lower(),
        ai_request_timeout_seconds=read_int("AI_REQUEST_TIMEOUT_SECONDS", 60),
        deepseek_api_key=read("DEEPSEEK_API_KEY"),
        deepseek_base_url=read("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        openai_api_key=read("OPENAI_API_KEY"),
        openai_image_api_key=read("OPENAI_IMAGE_API_KEY"),
        openai_base_url=read("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        kling_api_key=read_any(KLING_API_KEY_NAMES),
        kling_base_url=read(
            "KLING_BASE_URL", "https://api-singapore.klingai.com"
        ).rstrip("/"),
        kling_poll_interval_seconds=read_int("KLING_POLL_INTERVAL_SECONDS", 10),
        kling_poll_timeout_seconds=read_int("KLING_POLL_TIMEOUT_SECONDS", 600),
        yookassa_shop_id=read("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=read("YOOKASSA_SECRET_KEY"),
        yookassa_api_base_url=read(
            "YOOKASSA_API_BASE_URL", "https://api.yookassa.ru/v3"
        ).rstrip("/"),
        yookassa_webhook_path=read(
            "YOOKASSA_WEBHOOK_PATH", "/payments/yookassa/webhook"
        ),
        yookassa_return_path=read(
            "YOOKASSA_RETURN_PATH", "/payments/yookassa/return"
        ),
        yookassa_request_timeout_seconds=read_int(
            "YOOKASSA_REQUEST_TIMEOUT_SECONDS", 15
        ),
        platega_merchant_id=read("PLATEGA_MERCHANT_ID").strip(),
        platega_secret=read("PLATEGA_SECRET"),
        platega_api_base_url=_normalize_base_url(
            read("PLATEGA_API_BASE_URL", "https://app.platega.io")
        ),
        platega_webhook_path=read(
            "PLATEGA_WEBHOOK_PATH", "/payments/platega/webhook"
        ),
        platega_return_path=read(
            "PLATEGA_RETURN_PATH", "/payments/platega/return"
        ),
        platega_failed_path=read(
            "PLATEGA_FAILED_PATH", "/payments/platega/failed"
        ),
        platega_request_timeout_seconds=read_int(
            "PLATEGA_REQUEST_TIMEOUT_SECONDS", 30
        ),
        crypto_pay_token=read("CRYPTO_PAY_TOKEN"),
        crypto_pay_api_base_url=read(
            "CRYPTO_PAY_API_BASE",
            "https://testnet-pay.crypt.bot/api",
        ).rstrip("/"),
        crypto_pay_webhook_secret=read("CRYPTO_PAY_WEBHOOK_SECRET"),
        crypto_pay_webhook_path=read(
            "CRYPTO_PAY_WEBHOOK_PATH", "/payments/crypto/webhook"
        ),
        crypto_pay_accepted_assets=read("CRYPTO_PAY_ACCEPTED_ASSETS", "USDT"),
        crypto_pay_request_timeout_seconds=read_int(
            "CRYPTO_PAY_REQUEST_TIMEOUT_SECONDS", 15
        ),
        telegram_stars_amount=read_int("TELEGRAM_STARS_AMOUNT", 0),
        allow_ephemeral_sqlite=read_bool("CEAI_ALLOW_EPHEMERAL_SQLITE"),
    )
