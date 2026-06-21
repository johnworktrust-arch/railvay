from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ProviderError, ProviderResult


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
    ) -> ProviderResult:
        config = loads_dict(model.get("config"))
        model_key = str(config.get("api_model") or model["model_key"])
        instructions = (
            "Ты полезный AI-помощник внутри Telegram-бота CeaAI. "
            "Отвечай кратко, понятно и по-русски, если пользователь не попросил иначе."
        )
        if system_prompt:
            instructions = f"{instructions}\n\n{system_prompt.strip()}"
        payload = {
            "model": model_key,
            "reasoning": {"effort": str(config.get("reasoning_effort", "low"))},
            "instructions": instructions,
            "input": prompt_text.strip(),
        }
        raw = self._post_json("/responses", payload)
        text = self._extract_text(raw)
        response_id = str(raw.get("id") or f"openai-{model_key}")
        return ProviderResult(
            provider_job_id=response_id,
            result={"kind": "text", "text": text},
            provider_cost_amount=float(config.get("provider_cost_amount", 0)),
            provider_cost_currency=str(config.get("provider_cost_currency", "USD")),
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
