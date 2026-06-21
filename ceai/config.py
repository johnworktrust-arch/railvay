from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PUBLIC_OFFER_URL = "https://ceaai-bot.onrender.com/public-offer"
DEFAULT_INFO_CHANNEL_URL = "https://t.me/cea_family"


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


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_url: str
    app_env: str
    mock_payment_base_url: str
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
    openai_base_url: str = "https://api.openai.com/v1"


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

    return Settings(
        telegram_bot_token=read("TELEGRAM_BOT_TOKEN"),
        database_url=read("DATABASE_URL", "sqlite:///./data/ceai.sqlite3"),
        app_env=read("APP_ENV", "local"),
        mock_payment_base_url=read(
            "MOCK_PAYMENT_BASE_URL", "https://mock-payments.local/pay"
        ),
        app_base_url=read("APP_BASE_URL"),
        telegram_webhook_path=read("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook"),
        telegram_webhook_secret=read("TELEGRAM_WEBHOOK_SECRET"),
        admin_telegram_ids=read_int_list("ADMIN_TELEGRAM_IDS"),
        admin_telegram_usernames=read_username_list("ADMIN_TELEGRAM_USERNAMES"),
        public_offer_url=read("PUBLIC_OFFER_URL", DEFAULT_PUBLIC_OFFER_URL),
        info_channel_url=read("INFO_CHANNEL_URL", DEFAULT_INFO_CHANNEL_URL),
        support_username=read("SUPPORT_USERNAME", "cea_help").strip().lstrip("@"),
        ai_provider_mode=read("AI_PROVIDER_MODE", "auto").strip().lower(),
        ai_request_timeout_seconds=read_int("AI_REQUEST_TIMEOUT_SECONDS", 60),
        deepseek_api_key=read("DEEPSEEK_API_KEY"),
        deepseek_base_url=read("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        openai_api_key=read("OPENAI_API_KEY"),
        openai_base_url=read("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
