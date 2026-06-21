from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any, Dict


_STATE: Dict[str, Any] = {
    "last_message": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_message(*, handler: str, message: Any) -> None:
    from_user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    text = (getattr(message, "text", None) or "").strip()
    _STATE["last_message"] = {
        "at": _now(),
        "handler": handler,
        "message_id": getattr(message, "message_id", None),
        "chat_id": getattr(chat, "id", None),
        "chat_type": getattr(chat, "type", None),
        "from_id": getattr(from_user, "id", None),
        "username": getattr(from_user, "username", None),
        "text": text[:120],
    }


def record_error(*, exception: BaseException, update: Any = None) -> None:
    update_id = getattr(update, "update_id", None)
    _STATE["last_error"] = {
        "at": _now(),
        "update_id": update_id,
        "type": type(exception).__name__,
        "message": str(exception),
        "traceback": "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )[-4000:],
    }


def snapshot() -> Dict[str, Any]:
    return {
        "last_message": _STATE.get("last_message"),
        "last_error": _STATE.get("last_error"),
    }
