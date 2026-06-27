from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")
MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_datetime_minute(value: Any) -> str:
    if not value:
        return "—"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(DISPLAY_TIMEZONE)
    return parsed.strftime("%d.%m.%Y %H:%M")


def format_datetime_russian_minute(value: Any) -> str:
    if not value:
        return "—"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(DISPLAY_TIMEZONE)
    month = MONTHS_GENITIVE[parsed.month]
    return f"{parsed.day} {month} {parsed.year} года, {parsed:%H:%M}"


def format_coin_amount(amount: Any) -> str:
    value = int(amount or 0)
    abs_value = abs(value)
    if abs_value % 100 in {11, 12, 13, 14}:
        word = "коинов"
    elif abs_value % 10 == 1:
        word = "коин"
    elif abs_value % 10 in {2, 3, 4}:
        word = "коина"
    else:
        word = "коинов"
    return f"{value} {word}"
