from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ImageInput, ProviderError, ProviderResult


class OpenAIImageProvider:
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
        payload: Dict[str, Any] = {
            "model": model_key,
            "prompt": prompt,
            "n": int(config.get("n", 1)),
        }
        for key in ("quality", "size", "output_format", "background", "moderation"):
            value = config.get(key)
            if value:
                payload[key] = value

        if image_input is not None:
            payload["images"] = [
                {
                    "image_url": (
                        f"data:{image_input.mime_type};base64,"
                        f"{_base64_image(image_input.data)}"
                    )
                }
            ]
            raw = self._post_json("/images/edits", payload)
        else:
            raw = self._post_json("/images/generations", payload)

        data = raw.get("data")
        if not isinstance(data, list) or not data:
            raise ProviderError("OpenAI Image API returned no image data")
        first_image = data[0]
        if not isinstance(first_image, dict):
            raise ProviderError("OpenAI Image API returned malformed image data")

        image_b64 = first_image.get("b64_json")
        if not isinstance(image_b64, str) or not image_b64:
            raise ProviderError("OpenAI Image API returned no base64 image")

        image_hash = hashlib.sha1(image_b64[:4096].encode("ascii")).hexdigest()[:12]
        created = str(raw.get("created") or "0")
        output_format = str(raw.get("output_format") or payload.get("output_format") or "png")
        quality = str(raw.get("quality") or payload.get("quality") or "auto")
        size = str(raw.get("size") or payload.get("size") or "auto")
        caption_prefix = "Изменение изображения по запросу" if image_input else "Изображение по запросу"
        return ProviderResult(
            provider_job_id=f"openai-image-{created}-{image_hash}",
            result={
                "kind": "image",
                "image_b64": image_b64,
                "mime_type": f"image/{output_format}",
                "file_name": f"cea-ai-{image_hash}.{output_format}",
                "caption": f"{caption_prefix}: {prompt}",
                "model": model_key,
                "quality": quality,
                "size": size,
                "output_format": output_format,
                "revised_prompt": first_image.get("revised_prompt"),
                "usage": raw.get("usage"),
            },
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
                f"OpenAI Image API returned HTTP {exc.code}: {error_body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"OpenAI Image API request failed: {exc}") from exc


def _base64_image(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
