from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")


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
