from __future__ import annotations

from typing import Any, Dict

from ceai.config import KLING_API_KEY_NAMES, Settings
from ceai.database import Database
from ceai.providers.base import AIProvider, ImageInput, ProviderError, ProviderResult
from ceai.providers.deepseek import DeepSeekProvider
from ceai.providers.kling_video import KlingVideoProvider
from ceai.providers.mock import MockAIProvider
from ceai.providers.openai_image import OpenAIImageProvider
from ceai.providers.openai_text import OpenAITextProvider
from ceai.providers.openai_tts import OpenAITTSProvider
from ceai.repositories.app_settings import AppSettingsRepository


PROVIDER_SETTING_KEYS = (
    "AI_PROVIDER_MODE",
    "AI_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_IMAGE_API_KEY",
    "OPENAI_BASE_URL",
    *KLING_API_KEY_NAMES,
    "KLING_BASE_URL",
    "KLING_POLL_INTERVAL_SECONDS",
    "KLING_POLL_TIMEOUT_SECONDS",
)


class AIProviderRouter:
    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db
        self.mock = MockAIProvider()
        self.ai_provider_mode = settings.ai_provider_mode
        self.deepseek: AIProvider | None = None
        self.openai: AIProvider | None = None
        self.openai_image: AIProvider | None = None
        self.openai_tts: AIProvider | None = None
        self.kling_video: AIProvider | None = None
        self.reload_settings()

    def reload_settings(self) -> None:
        saved_settings = self._load_saved_settings(self.db)
        self.ai_provider_mode = (
            saved_settings.get("AI_PROVIDER_MODE") or self.settings.ai_provider_mode
        ).strip().lower()
        timeout_seconds = self._read_timeout(self.settings, saved_settings)
        deepseek_api_key = self.settings.deepseek_api_key or saved_settings.get(
            "DEEPSEEK_API_KEY", ""
        )
        deepseek_base_url = (
            saved_settings.get("DEEPSEEK_BASE_URL") or self.settings.deepseek_base_url
        )
        openai_api_key = self.settings.openai_api_key or saved_settings.get(
            "OPENAI_API_KEY", ""
        )
        openai_image_api_key = (
            self.settings.openai_image_api_key
            or saved_settings.get("OPENAI_IMAGE_API_KEY", "")
            or openai_api_key
        )
        openai_base_url = (
            saved_settings.get("OPENAI_BASE_URL") or self.settings.openai_base_url
        )
        kling_api_key = self.settings.kling_api_key or self._read_saved_setting_any(
            saved_settings, KLING_API_KEY_NAMES
        )
        kling_base_url = (
            saved_settings.get("KLING_BASE_URL") or self.settings.kling_base_url
        )
        kling_poll_interval_seconds = self._read_int_setting(
            saved_settings,
            "KLING_POLL_INTERVAL_SECONDS",
            self.settings.kling_poll_interval_seconds,
        )
        kling_poll_timeout_seconds = self._read_int_setting(
            saved_settings,
            "KLING_POLL_TIMEOUT_SECONDS",
            self.settings.kling_poll_timeout_seconds,
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
        self.openai_image = (
            OpenAIImageProvider(
                api_key=openai_image_api_key,
                base_url=openai_base_url,
                timeout_seconds=timeout_seconds,
            )
            if openai_image_api_key
            else None
        )
        self.openai_tts = (
            OpenAITTSProvider(
                api_key=openai_api_key,
                base_url=openai_base_url,
                timeout_seconds=timeout_seconds,
            )
            if openai_api_key
            else None
        )
        self.kling_video = (
            KlingVideoProvider(
                api_key=kling_api_key,
                base_url=kling_base_url,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=kling_poll_interval_seconds,
                poll_timeout_seconds=kling_poll_timeout_seconds,
            )
            if kling_api_key
            else None
        )

    def generate(
        self,
        *,
        model: Dict[str, Any],
        prompt_text: str,
        system_prompt: str | None = None,
        image_input: ImageInput | None = None,
    ) -> ProviderResult:
        self.reload_settings()
        provider = self._provider_for(model)
        return provider.generate(
            model=model,
            prompt_text=prompt_text,
            system_prompt=system_prompt,
            image_input=image_input,
        )

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
        elif provider_key == "openai" and generation_type == "image":
            real_provider = self.openai_image
        elif provider_key == "openai" and generation_type == "tts":
            real_provider = self.openai_tts
        elif provider_key == "kling" and generation_type == "video":
            real_provider = self.kling_video

        if real_provider is not None:
            return real_provider
        if provider_key == "deepseek" and generation_type == "text":
            if mode == "auto" and self.settings.app_env.strip().lower() == "test":
                return self.mock
            raise ProviderError(
                "DeepSeek provider is not configured. Set DEEPSEEK_API_KEY."
            )
        if provider_key == "openai" and generation_type == "text":
            if mode == "auto" and self.settings.app_env.strip().lower() == "test":
                return self.mock
            raise ProviderError(
                "OpenAI text provider is not configured. Set OPENAI_API_KEY."
            )
        if provider_key == "openai" and generation_type == "image":
            raise ProviderError(
                "OpenAI Image provider is not configured. "
                "Set OPENAI_IMAGE_API_KEY or OPENAI_API_KEY."
            )
        if provider_key == "kling" and generation_type == "video":
            raise ProviderError(
                "Kling video provider is not configured. Set KLING_API_KEY."
            )
        if provider_key == "openai" and generation_type == "tts":
            raise ProviderError(
                "OpenAI TTS provider is not configured. Set OPENAI_API_KEY."
            )
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

    def _read_int_setting(
        self, saved_settings: Dict[str, str], key: str, default: int
    ) -> int:
        raw = saved_settings.get(key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return default

    def _read_saved_setting_any(
        self, saved_settings: Dict[str, str], keys: tuple[str, ...]
    ) -> str:
        for key in keys:
            value = saved_settings.get(key, "")
            if value.strip():
                return value.strip()
        normalized = {key.strip().upper().rstrip(";") for key in keys}
        for key, value in saved_settings.items():
            if key.strip().upper().rstrip(";") in normalized and value.strip():
                return value.strip()
        return ""
