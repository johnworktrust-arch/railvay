from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    provider_job_id: str
    result: Dict[str, Any]
    provider_cost_amount: float
    provider_cost_currency: str
    duration_seconds: int | None = None


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime_type: str
    file_name: str


class AIProvider(Protocol):
    def generate(
        self,
        *,
        model: Dict[str, Any],
        prompt_text: str,
        system_prompt: str | None = None,
        image_input: ImageInput | None = None,
    ) -> ProviderResult:
        ...
