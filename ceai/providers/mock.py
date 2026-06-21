from __future__ import annotations

import hashlib
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ProviderError, ProviderResult


class MockProviderError(ProviderError):
    pass


class MockAIProvider:
    def generate(
        self,
        *,
        model: Dict[str, Any],
        prompt_text: str,
        system_prompt: str | None = None,
    ) -> ProviderResult:
        normalized_prompt = prompt_text.strip()
        if "mock_error" in normalized_prompt.lower():
            raise MockProviderError("Mock AI provider returned an error")

        generation_type = model["generation_type"]
        config = loads_dict(model.get("config"))
        job_hash = hashlib.sha1(
            f"{model['provider']}:{model['model_key']}:{normalized_prompt}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        provider_job_id = f"mock-{generation_type}-{job_hash}"
        cost_amount = float(config.get("provider_cost_amount", 0))
        cost_currency = str(config.get("provider_cost_currency", "RUB"))

        if generation_type == "text":
            result = {
                "kind": "text",
                "text": (
                    f"Mock-ответ от {model['display_name']}: "
                    f"я получил запрос «{normalized_prompt}»."
                ),
            }
            duration = None
        elif generation_type == "image":
            result = {
                "kind": "image",
                "url": f"https://example.com/ceaai/mock-image-{job_hash}.png",
                "caption": f"Тестовое изображение по запросу: {normalized_prompt}",
            }
            duration = None
        elif generation_type == "video":
            result = {
                "kind": "video",
                "url": f"https://example.com/ceaai/mock-video-{job_hash}.mp4",
                "caption": f"Тестовое видео по запросу: {normalized_prompt}",
            }
            duration = int(config.get("duration_seconds", 10))
        elif generation_type == "tts":
            result = {
                "kind": "tts",
                "url": f"https://example.com/ceaai/mock-voice-{job_hash}.mp3",
                "message": f"Тестовая озвучка текста: {normalized_prompt}",
            }
            duration = int(config.get("duration_seconds", 15))
        else:
            result = {
                "kind": generation_type,
                "message": f"Mock result for {normalized_prompt}",
            }
            duration = None

        return ProviderResult(
            provider_job_id=provider_job_id,
            result=result,
            provider_cost_amount=cost_amount,
            provider_cost_currency=cost_currency,
            duration_seconds=duration,
        )
