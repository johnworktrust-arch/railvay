from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ImageInput, ProviderError, ProviderResult
from ceai.providers.identity import text_model_instructions


class OpenAITextProvider:
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
        prompt = prompt_text.strip()
        max_input_characters = int(config.get("max_input_characters") or 6000)
        if len(prompt) > max_input_characters:
            raise ProviderError(
                f"OpenAI input cannot exceed {max_input_characters} characters"
            )
        instructions = text_model_instructions(model, system_prompt=system_prompt)
        payload = {
            "model": model_key,
            "reasoning": {"effort": str(config.get("reasoning_effort", "low"))},
            "instructions": instructions,
            "input": prompt,
            "max_output_tokens": int(config.get("max_output_tokens") or 1500),
        }
        raw = self._post_json("/responses", payload)
        text = self._extract_text(raw)
        response_id = str(raw.get("id") or f"openai-{model_key}")
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
                f"OpenAI API returned HTTP {exc.code}: {error_body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"OpenAI API request failed: {exc}") from exc

    @staticmethod
    def _extract_text(raw: Dict[str, Any]) -> str:
        output_text = raw.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: list[str] = []
        for item in raw.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts)

        raise ProviderError("OpenAI API returned no text output")


def _text_cost_usd(config: Dict[str, Any], usage: Dict[str, Any]) -> float:
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    input_details = usage.get("input_tokens_details")
    cached_tokens = (
        max(0, int(input_details.get("cached_tokens") or 0))
        if isinstance(input_details, dict)
        else 0
    )
    cached_tokens = min(cached_tokens, input_tokens)
    regular_tokens = input_tokens - cached_tokens
    if not input_tokens and not output_tokens:
        return float(config.get("fallback_cost_usd") or 0.06)
    return round(
        (
            regular_tokens * float(config.get("input_cost_per_million_usd") or 5.0)
            + cached_tokens
            * float(config.get("cached_input_cost_per_million_usd") or 0.5)
            + output_tokens * float(config.get("output_cost_per_million_usd") or 30.0)
        )
        / 1_000_000,
        8,
    )
