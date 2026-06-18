from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


BASE_DIR = Path(__file__).resolve().parent.parent


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


def load_settings() -> Settings:
    dotenv_values = _load_dotenv(BASE_DIR / ".env")

    def read(name: str, default: str = "") -> str:
        return os.getenv(name) or dotenv_values.get(name, default)

    return Settings(
        telegram_bot_token=read("TELEGRAM_BOT_TOKEN"),
        database_url=read("DATABASE_URL", "sqlite:///./data/ceai.sqlite3"),
        app_env=read("APP_ENV", "local"),
        mock_payment_base_url=read(
            "MOCK_PAYMENT_BASE_URL", "https://mock-payments.local/pay"
        ),
    )
