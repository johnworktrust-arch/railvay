from __future__ import annotations

from typing import Any, Dict

from ceai.config import Settings
from ceai.database import Database
from ceai.providers.base import AIProvider, ProviderError, ProviderResult
from ceai.providers.deepseek import DeepSeekProvider
from ceai.providers.mock import MockAIProvider
from ceai.providers.openai_text import OpenAITextProvider
from ceai.repositories.app_settings import AppSettingsRepository


PROVIDER_SETTING_KEYS = (
    "AI_PROVIDER_MODE",
    "AI_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


class AIProviderRouter:
    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.mock = MockAIProvider()
        saved_settings = self._load_saved_settings(db)
        self.ai_provider_mode = (
            saved_settings.get("AI_PROVIDER_MODE") or settings.ai_provider_mode
        ).strip().lower()
        timeout_seconds = self._read_timeout(settings, saved_settings)
        deepseek_api_key = settings.deepseek_api_key or saved_settings.get(
            "DEEPSEEK_API_KEY", ""
        )
        deepseek_base_url = (
            saved_settings.get("DEEPSEEK_BASE_URL") or settings.deepseek_base_url
        )
        openai_api_key = settings.openai_api_key or saved_settings.get(
            "OPENAI_API_KEY", ""
        )
        openai_base_url = (
            saved_settings.get("OPENAI_BASE_URL") or settings.openai_base_url
        )
        self.deepseek = (
            DeepSeekProvider(
                api_key=deepseek_api_key,
                base_url=deepseek_base_url,
                timeout_seconds=timeout_seconds,
            )
            if deepseek_api_key
            else None
        )
        self.openai = (
            OpenAITextProvider(
                api_key=openai_api_key,
                base_url=openai_base_url,
                timeout_seconds=timeout_seconds,
            )
            if openai_api_key
            else None
        )

    def generate(self, *, model: Dict[str, Any], prompt_text: str) -> ProviderResult:
        provider = self._provider_for(model)
        return provider.generate(model=model, prompt_text=prompt_text)

    def _provider_for(self, model: Dict[str, Any]) -> AIProvider:
        mode = self.ai_provider_mode
        if mode == "mock":
            return self.mock

        provider_key = str(model.get("provider") or "")
        generation_type = str(model.get("generation_type") or "")
        real_provider: AIProvider | None = None

        if provider_key == "deepseek" and generation_type == "text":
            real_provider = self.deepseek
        elif provider_key == "openai" and generation_type == "text":
            real_provider = self.openai

        if real_provider is not None:
            return real_provider
        if mode == "real":
            raise ProviderError(
                f"Real provider is not configured for {provider_key}/{generation_type}"
            )
        return self.mock

    def _load_saved_settings(self, db: Database | None) -> Dict[str, str]:
        if db is None:
            return {}
        try:
            return AppSettingsRepository().get_many(db.conn, PROVIDER_SETTING_KEYS)
        except Exception:
            return {}

    def _read_timeout(self, settings: Settings, saved_settings: Dict[str, str]) -> int:
        raw = saved_settings.get("AI_REQUEST_TIMEOUT_SECONDS")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return settings.ai_request_timeout_seconds
