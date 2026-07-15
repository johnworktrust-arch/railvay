from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ImageInput, ProviderError, ProviderResult
from ceai.providers.identity import text_model_instructions


class DeepSeekProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
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
        prompt = prompt_text.strip()
        max_input_characters = int(config.get("max_input_characters") or 6000)
        if len(prompt) > max_input_characters:
            raise ProviderError(
                f"DeepSeek input cannot exceed {max_input_characters} characters"
            )
        instructions = text_model_instructions(model, system_prompt=system_prompt)
        payload = {
            "model": model_key,
            "messages": [
                {
                    "role": "system",
                    "content": instructions,
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "thinking": {"type": str(config.get("thinking_type", "disabled"))},
            "max_tokens": int(config.get("max_output_tokens") or 2000),
        }
        raw = self._post_json("/chat/completions", payload)
        text = self._extract_text(raw)
        response_id = str(raw.get("id") or f"deepseek-{model_key}")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return ProviderResult(
            provider_job_id=response_id,
            result={"kind": "text", "text": text, "usage": usage},
            provider_cost_amount=_text_cost_usd(config, usage),
            provider_cost_currency="USD",
        )

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"DeepSeek API returned HTTP {exc.code}: {error_body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"DeepSeek API request failed: {exc}") from exc

    @staticmethod
    def _extract_text(raw: Dict[str, Any]) -> str:
        choices = raw.get("choices") or []
        if not choices:
            raise ProviderError("DeepSeek API returned no choices")
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("DeepSeek API returned no text content")
        return text.strip()


def _text_cost_usd(config: Dict[str, Any], usage: Dict[str, Any]) -> float:
    prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
    completion_tokens = max(0, int(usage.get("completion_tokens") or 0))
    cache_hit_tokens = max(0, int(usage.get("prompt_cache_hit_tokens") or 0))
    cache_hit_tokens = min(cache_hit_tokens, prompt_tokens)
    cache_miss_tokens = max(
        0,
        int(usage.get("prompt_cache_miss_tokens") or prompt_tokens - cache_hit_tokens),
    )
    cache_miss_tokens = min(cache_miss_tokens, prompt_tokens - cache_hit_tokens)
    if not prompt_tokens and not completion_tokens:
        return float(config.get("fallback_cost_usd") or 0.001)
    return round(
        (
            cache_miss_tokens
            * float(config.get("input_cost_per_million_usd") or 0.14)
            + cache_hit_tokens
            * float(config.get("cached_input_cost_per_million_usd") or 0.0028)
            + completion_tokens
            * float(config.get("output_cost_per_million_usd") or 0.28)
        )
        / 1_000_000,
        8,
    )
