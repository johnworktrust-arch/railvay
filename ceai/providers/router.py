from __future__ import annotations

from typing import Any, Dict

from ceai.config import Settings
from ceai.providers.base import AIProvider, ProviderError, ProviderResult
from ceai.providers.deepseek import DeepSeekProvider
from ceai.providers.mock import MockAIProvider
from ceai.providers.openai_text import OpenAITextProvider


class AIProviderRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mock = MockAIProvider()
        self.deepseek = (
            DeepSeekProvider(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.ai_request_timeout_seconds,
            )
            if settings.deepseek_api_key
            else None
        )
        self.openai = (
            OpenAITextProvider(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=settings.ai_request_timeout_seconds,
            )
            if settings.openai_api_key
            else None
        )

    def generate(self, *, model: Dict[str, Any], prompt_text: str) -> ProviderResult:
        provider = self._provider_for(model)
        return provider.generate(model=model, prompt_text=prompt_text)

    def _provider_for(self, model: Dict[str, Any]) -> AIProvider:
        mode = self.settings.ai_provider_mode
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
