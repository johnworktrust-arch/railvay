from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ImageInput, ProviderError, ProviderResult


SUPPORTED_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}
SUPPORTED_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}


class OpenAITTSProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int = 60,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate(
        self,
        *,
        model: Dict[str, Any],
        prompt_text: str,
        system_prompt: str | None = None,
        image_input: ImageInput | None = None,
    ) -> ProviderResult:
        config = loads_dict(model.get("config"))
        model_key = str(config.get("api_model") or model["model_key"])
        voice = str(config.get("voice") or "alloy")
        response_format = str(config.get("response_format") or "mp3")
        prompt = prompt_text.strip()
        if not prompt:
            raise ProviderError("OpenAI TTS input cannot be empty")
        if len(prompt) > 4096:
            raise ProviderError("OpenAI TTS input cannot exceed 4096 characters")
        if voice not in SUPPORTED_VOICES:
            raise ProviderError(f"Unsupported OpenAI TTS voice: {voice}")
        if response_format not in SUPPORTED_FORMATS:
            raise ProviderError(f"Unsupported OpenAI TTS format: {response_format}")
        payload: Dict[str, Any] = {
            "model": model_key,
            "voice": voice,
            "input": prompt,
            "response_format": response_format,
        }
        speed = config.get("speed")
        if speed is not None:
            payload["speed"] = float(speed)

        audio = self._post_audio("/audio/speech", payload)
        audio_hash = hashlib.sha1(audio).hexdigest()[:12]
        mime_type = "audio/mpeg" if response_format == "mp3" else f"audio/{response_format}"
        input_characters = len(prompt)
        cost_usd = round(
            input_characters
            * float(config.get("cost_per_million_characters_usd") or 15.0)
            / 1_000_000,
            8,
        )
        return ProviderResult(
            provider_job_id=f"openai-tts-{audio_hash}",
            result={
                "kind": "tts",
                "audio_b64": base64.b64encode(audio).decode("ascii"),
                "mime_type": mime_type,
                "file_name": f"cea-ai-voice-{audio_hash}.{response_format}",
                "message": "Озвучка готова.",
                "model": model_key,
                "voice": voice,
                "usage": {"input_characters": input_characters},
            },
            provider_cost_amount=cost_usd,
            provider_cost_currency="USD",
            duration_seconds=(
                int(config["duration_seconds"])
                if config.get("duration_seconds") is not None
                else None
            ),
        )

    def _post_audio(self, path: str, payload: Dict[str, Any]) -> bytes:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"OpenAI TTS API returned HTTP {exc.code}: {error_body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f"OpenAI TTS API request failed: {exc}") from exc
        if not audio:
            raise ProviderError("OpenAI TTS API returned empty audio")
        return audio
