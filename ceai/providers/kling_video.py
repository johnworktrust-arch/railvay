from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict

from ceai.json_utils import loads_dict
from ceai.providers.base import ImageInput, ProviderError, ProviderResult


class KlingVideoProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api-singapore.klingai.com",
        timeout_seconds: int = 60,
        poll_interval_seconds: int = 10,
        poll_timeout_seconds: int = 600,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = max(3, poll_interval_seconds)
        self.poll_timeout_seconds = max(30, poll_timeout_seconds)

    def generate(
        self,
        *,
        model: Dict[str, Any],
        prompt_text: str,
        system_prompt: str | None = None,
        image_input: ImageInput | None = None,
    ) -> ProviderResult:
        if str(model.get("generation_type") or "") != "video":
            raise ProviderError("Kling provider supports only video generation")
        if image_input is not None:
            raise ProviderError("Kling image-to-video is not connected yet")

        config = loads_dict(model.get("config"))
        prompt = prompt_text.strip()
        if not prompt:
            raise ProviderError("Kling prompt is empty")

        duration_seconds = int(config.get("duration_seconds") or 5)
        payload = {
            "model_name": str(config.get("api_model") or "kling-v3"),
            "prompt": prompt,
            "negative_prompt": str(config.get("negative_prompt") or ""),
            "duration": str(duration_seconds),
            "mode": str(config.get("mode") or "std"),
            "sound": str(config.get("sound") or "off"),
            "aspect_ratio": str(config.get("aspect_ratio") or "16:9"),
        }
        external_task_id = str(config.get("external_task_id") or "").strip()
        if external_task_id:
            payload["external_task_id"] = external_task_id

        created = self._request_json("POST", "/v1/videos/text2video", payload=payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise ProviderError("Kling API returned no task_id")

        completed = self._poll_task(task_id)
        video = _extract_video(completed)
        video_url = str(video.get("url") or video.get("watermark_url") or "").strip()
        if not video_url:
            raise ProviderError("Kling API returned no video URL")
        actual_duration = _read_duration(video, fallback=duration_seconds)
        units_per_second = float(config.get("resource_units_per_second") or 0.6)
        resource_units = round(actual_duration * units_per_second, 4)
        provider_cost_usd = round(
            resource_units * float(config.get("resource_unit_cost_usd") or 0.098),
            8,
        )

        return ProviderResult(
            provider_job_id=task_id,
            result={
                "kind": "video",
                "url": video_url,
                "caption": f"Готово: видео по запросу «{prompt}».",
                "duration_seconds": actual_duration,
                "kling_task_id": task_id,
                "usage": {"resource_units": resource_units},
            },
            provider_cost_amount=provider_cost_usd,
            provider_cost_currency="USD",
            duration_seconds=actual_duration,
        )

    def _poll_task(self, task_id: str) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        last_payload: Dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            raw = self._request_json("GET", f"/v1/videos/text2video/{task_id}")
            last_payload = raw
            data = _extract_data(raw)
            status = str(data.get("task_status") or "").strip().lower()
            if status == "succeed":
                return raw
            if status == "failed":
                message = str(data.get("task_status_msg") or raw.get("message") or "")
                raise ProviderError(f"Kling task failed: {message or 'unknown error'}")
            time.sleep(self.poll_interval_seconds)

        status_text = ""
        if last_payload:
            data = _extract_data(last_payload)
            status_text = str(data.get("task_status") or "").strip()
        raise ProviderError(
            "Kling video generation timed out"
            + (f" with status {status_text}" if status_text else "")
        )

    def _request_json(
        self, method: str, path: str, *, payload: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"Kling API returned HTTP {exc.code}: {error_body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Kling API request failed: {exc}") from exc

        if not isinstance(raw, dict):
            raise ProviderError("Kling API returned malformed response")
        code = raw.get("code")
        if code not in (None, 0, "0"):
            message = str(raw.get("message") or "unknown error")
            raise ProviderError(f"Kling API rejected request: {message}")
        return raw


def _extract_data(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = raw.get("data")
    return data if isinstance(data, dict) else {}


def _extract_task_id(raw: Dict[str, Any]) -> str:
    data = _extract_data(raw)
    return str(data.get("task_id") or data.get("id") or "").strip()


def _extract_video(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = _extract_data(raw)
    task_result = data.get("task_result")
    if isinstance(task_result, dict):
        videos = task_result.get("videos")
        if isinstance(videos, list) and videos:
            first = videos[0]
            if isinstance(first, dict):
                return first
    videos = data.get("videos")
    if isinstance(videos, list) and videos and isinstance(videos[0], dict):
        return videos[0]
    raise ProviderError("Kling API returned no videos in task_result")


def _read_duration(video: Dict[str, Any], *, fallback: int) -> int:
    try:
        return int(float(str(video.get("duration") or fallback)))
    except (TypeError, ValueError):
        return fallback
