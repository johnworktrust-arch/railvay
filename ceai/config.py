from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PUBLIC_OFFER_URL = "https://cea.ai/public-offer"
DEFAULT_INFO_CHANNEL_URL = "https://t.me/ceafamily"


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
class Settings:
    telegram_bot_token: str
    database_url: str
    app_env: str
    mock_payment_base_url: str
    payment_provider: str = "mock"
    app_base_url: str = ""
    telegram_webhook_path: str = "/telegram/webhook"
    telegram_webhook_secret: str = ""
    admin_telegram_ids: Tuple[int, ...] = ()
    admin_telegram_usernames: Tuple[str, ...] = ()
    public_offer_url: str = DEFAULT_PUBLIC_OFFER_URL
    info_channel_url: str = DEFAULT_INFO_CHANNEL_URL
    support_username: str = "cea_help"
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

    app_base_url = _normalize_base_url(
        read("APP_BASE_URL") or read("RAILWAY_PUBLIC_DOMAIN")
    )
    public_offer_default = (
        f"{app_base_url}/public-offer" if app_base_url else DEFAULT_PUBLIC_OFFER_URL
    )

    return Settings(
        telegram_bot_token=read("TELEGRAM_BOT_TOKEN"),
        database_url=read("DATABASE_URL", "sqlite:///./data/ceai.sqlite3"),
        app_env=read("APP_ENV", "local"),
        mock_payment_base_url=read(
            "MOCK_PAYMENT_BASE_URL", "https://mock-payments.local/pay"
        ),
        payment_provider=read("PAYMENT_PROVIDER", "mock").strip().lower(),
        app_base_url=app_base_url,
        telegram_webhook_path=read("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook"),
        telegram_webhook_secret=read("TELEGRAM_WEBHOOK_SECRET"),
        admin_telegram_ids=read_int_list("ADMIN_TELEGRAM_IDS"),
        admin_telegram_usernames=read_username_list("ADMIN_TELEGRAM_USERNAMES"),
        public_offer_url=read("PUBLIC_OFFER_URL", public_offer_default),
        info_channel_url=_normalize_telegram_url(
            read("INFO_CHANNEL_URL", DEFAULT_INFO_CHANNEL_URL)
        ),
        support_username=read("SUPPORT_USERNAME", "cea_help").strip().lstrip("@"),
        ai_provider_mode=read("AI_PROVIDER_MODE", "auto").strip().lower(),
        ai_request_timeout_seconds=read_int("AI_REQUEST_TIMEOUT_SECONDS", 60),
        deepseek_api_key=read("DEEPSEEK_API_KEY"),
        deepseek_base_url=read("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        openai_api_key=read("OPENAI_API_KEY"),
        openai_image_api_key=read("OPENAI_IMAGE_API_KEY"),
        openai_base_url=read("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        kling_api_key=read("KLING_API_KEY"),
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
