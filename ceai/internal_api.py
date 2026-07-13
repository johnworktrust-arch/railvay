from __future__ import annotations

import hmac
import os
from json import JSONDecodeError
from typing import Mapping, Tuple

from ceai.config import Settings
from ceai.config import KLING_API_KEY_NAMES
from ceai.database import Database
from ceai.json_utils import dumps, loads_dict
from ceai.providers.base import ProviderError
from ceai.providers.router import AIProviderRouter, PROVIDER_SETTING_KEYS
from ceai.repositories.app_settings import AppSettingsRepository
from ceai.repositories.model_prices import ModelPriceRepository


SECRET_SETTING_KEYS = {
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_IMAGE_API_KEY",
    *KLING_API_KEY_NAMES,
}
TEXT_PROVIDER_MODELS = (
    ("deepseek", "deepseek-v4-flash"),
    ("openai", "gpt-4o-mini"),
)


def handle_provider_settings_request(
    *,
    settings: Settings,
    db: Database,
    headers: Mapping[str, str],
    body: bytes,
) -> Tuple[int, str, str]:
    if not _is_authorized(headers, settings.telegram_bot_token):
        return _json_response(401, {"ok": False, "error": "unauthorized"})

    try:
        payload = loads_dict(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError):
        return _json_response(400, {"ok": False, "error": "invalid_json"})

    values = payload.get("settings")
    if not isinstance(values, dict):
        return _json_response(400, {"ok": False, "error": "settings_required"})

    cleaned: dict[str, str] = {}
    for key in PROVIDER_SETTING_KEYS:
        value = values.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return _json_response(400, {"ok": False, "error": f"{key}_must_be_string"})
        value = value.strip()
        if value:
            cleaned[key] = value

    if not cleaned:
        return _json_response(400, {"ok": False, "error": "no_settings"})

    repo = AppSettingsRepository()
    with db.transaction() as conn:
        for key, value in cleaned.items():
            repo.upsert(
                conn,
                key=key,
                value=value,
                is_secret=key in SECRET_SETTING_KEYS,
            )

    result: dict[str, object] = {"ok": True, "saved": sorted(cleaned)}
    if payload.get("verify") is True:
        result["providers"] = _verify_text_providers(settings, db)
    return _json_response(200, result)


def handle_provider_status_request(
    *,
    settings: Settings,
    db: Database,
    headers: Mapping[str, str],
) -> Tuple[int, str, str]:
    if not _is_authorized(headers, settings.telegram_bot_token):
        return _json_response(401, {"ok": False, "error": "unauthorized"})

    router = AIProviderRouter(settings, db)
    model_repo = ModelPriceRepository()
    with db.transaction() as conn:
        kling_model = model_repo.get_by_provider_key(conn, "kling", "kling-3")

    return _json_response(
        200,
        {
            "ok": True,
            "ai_provider_mode": settings.ai_provider_mode,
            "providers": {
                "deepseek_text_configured": router.deepseek is not None,
                "openai_text_configured": router.openai is not None,
                "openai_image_configured": router.openai_image is not None,
                "openai_tts_configured": router.openai_tts is not None,
                "kling_video_configured": router.kling_video is not None,
            },
            "models": {
                "kling_3_active": bool(kling_model and kling_model["is_active"]),
                "kling_3_cost": (
                    int(kling_model["coins_cost"]) if kling_model else None
                ),
            },
            "diagnostics": {
                "kling_env_keys_present": _kling_env_key_names(),
                "supported_kling_key_names": list(KLING_API_KEY_NAMES),
            },
        },
    )


def _verify_text_providers(settings: Settings, db: Database) -> dict[str, str]:
    router = AIProviderRouter(settings, db)
    model_repo = ModelPriceRepository()
    provider_status: dict[str, str] = {}
    with db.transaction() as conn:
        models = [
            model_repo.get_by_provider_key(conn, provider, model_key)
            for provider, model_key in TEXT_PROVIDER_MODELS
        ]

    for model in models:
        if model is None:
            provider_status["unknown"] = "missing_model"
            continue
        provider_key = str(model["provider"])
        try:
            result = router.generate(model=model, prompt_text="Ответь одним словом: ок")
        except ProviderError as exc:
            provider_status[provider_key] = f"error:{exc.__class__.__name__}"
            continue
        text = result.result.get("text")
        provider_status[provider_key] = "ok" if text else "empty_response"
    return provider_status


def _is_authorized(headers: Mapping[str, str], expected_token: str) -> bool:
    if not expected_token:
        return False
    header_values = {key.lower(): value for key, value in headers.items()}
    auth = header_values.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = header_values.get("x-ceaai-admin-token", "").strip()
    return hmac.compare_digest(token, expected_token)


def _kling_env_key_names() -> list[str]:
    names = []
    for key in os.environ:
        normalized = key.strip().upper()
        if "KLING" in normalized or normalized in {"API_KEY_KLING"}:
            names.append(key)
    return sorted(names)


def _json_response(status: int, body: dict[str, object]) -> Tuple[int, str, str]:
    return status, "application/json", dumps(body) + "\n"
