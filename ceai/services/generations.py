from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from ceai.database import Database
from ceai.json_utils import loads_dict
from ceai.providers.base import AIProvider, ImageInput, ProviderError
from ceai.repositories.generations import GenerationRepository
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.runtime_diagnostics import record_error
from ceai.services.coins import CoinService
from ceai.services.exceptions import (
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)
from ceai.services.subscription_recovery import (
    recover_active_subscription_from_paid_payment,
)


@dataclass(frozen=True)
class GenerationResult:
    generation: Dict[str, Any]
    model: Dict[str, Any]
    result: Dict[str, Any]
    balance_after: int


class GenerationService:
    def __init__(self, db: Database, provider: AIProvider) -> None:
        self.db = db
        self.provider = provider
        self.models = ModelPriceRepository()
        self.subscriptions = SubscriptionRepository()
        self.generations = GenerationRepository()
        self.coins = CoinService()

    def generate(
        self,
        *,
        user_id: int,
        model_price_id: int,
        prompt_text: str,
        text_chat_id: int | None = None,
        text_chat_title: str | None = None,
        text_chat_system_prompt: str | None = None,
        image_input: ImageInput | None = None,
    ) -> GenerationResult:
        business_error: NoActiveSubscriptionError | InsufficientCoinsError | None = None
        with self.db.transaction() as conn:
            model = self.models.get_by_id(conn, model_price_id)
            if model is None or not model["is_active"]:
                raise NotFoundError("Модель не найдена")

            cost = _generation_coin_cost(model, prompt_text)
            prompt_payload: Dict[str, Any] = {"text": prompt_text}
            if _is_image_four_k_request(model, prompt_text):
                prompt_payload["image_resolution"] = "4k"
            if image_input is not None:
                prompt_payload["image_input"] = {
                    "file_name": image_input.file_name,
                    "mime_type": image_input.mime_type,
                    "size_bytes": len(image_input.data),
                }
            if text_chat_id is not None:
                prompt_payload["text_chat_id"] = text_chat_id
            if text_chat_title:
                prompt_payload["text_chat_title"] = text_chat_title
            if text_chat_system_prompt:
                prompt_payload["text_chat_system_prompt"] = text_chat_system_prompt

            generation = self.generations.create_pending(
                conn,
                user_id=user_id,
                model_price_id=model["id"],
                generation_type=model["generation_type"],
                provider=model["provider"],
                prompt=prompt_payload,
            )
            subscription = self.subscriptions.get_active_for_user(conn, user_id)
            if subscription is None:
                subscription = recover_active_subscription_from_paid_payment(
                    conn,
                    user_id=user_id,
                    subscriptions=self.subscriptions,
                    coins=self.coins,
                )
            if subscription is None:
                self.generations.mark_failed(
                    conn,
                    generation_id=generation["id"],
                    error_message="No active subscription",
                )
                business_error = NoActiveSubscriptionError(
                    "Нет активной подписки", generation_id=generation["id"]
                )
            else:
                balance = self.coins.balance_for_subscription(conn, subscription["id"])
                self.subscriptions.set_balance_cache(
                    conn, subscription_id=subscription["id"], balance=balance
                )
                if balance < cost:
                    self.generations.mark_failed(
                        conn,
                        generation_id=generation["id"],
                        subscription_id=subscription["id"],
                        error_message="Insufficient coins",
                    )
                    business_error = InsufficientCoinsError(
                        "Недостаточно монет", generation_id=generation["id"]
                    )
                else:
                    self.coins.reserve_generation(
                        conn,
                        user_id=user_id,
                        subscription_id=subscription["id"],
                        generation_id=generation["id"],
                        amount=cost,
                    )
                    self.generations.mark_processing(
                        conn,
                        generation_id=generation["id"],
                        subscription_id=subscription["id"],
                        coins_reserved=cost,
                    )

        if business_error is not None:
            raise business_error

        try:
            provider_result = self.provider.generate(
                model=model,
                prompt_text=prompt_text,
                system_prompt=text_chat_system_prompt,
                image_input=image_input,
            )
        except ProviderError as exc:
            record_error(exception=exc)
            with self.db.transaction() as conn:
                self.generations.mark_failed(
                    conn,
                    generation_id=generation["id"],
                    subscription_id=subscription["id"],
                    error_message=str(exc),
                )
                self.coins.refund_generation(
                    conn,
                    user_id=user_id,
                    subscription_id=subscription["id"],
                    generation_id=generation["id"],
                    amount=cost,
                )
                balance_after = self.coins.sync_subscription_cache(
                    conn, subscription["id"]
                )
            raise GenerationProviderFailedError(
                _provider_error_message(
                    provider_error=str(exc),
                    generation_type=str(model["generation_type"]),
                ),
                generation_id=generation["id"],
            ) from exc

        with self.db.transaction() as conn:
            self.coins.finalize_generation_charge(
                conn,
                user_id=user_id,
                subscription_id=subscription["id"],
                generation_id=generation["id"],
            )
            balance_after = self.coins.sync_subscription_cache(conn, subscription["id"])
            generation = self.generations.mark_completed(
                conn,
                generation_id=generation["id"],
                result=_result_for_storage(provider_result.result),
                provider_job_id=provider_result.provider_job_id,
                coins_charged=cost,
                provider_cost_amount=provider_result.provider_cost_amount,
                provider_cost_currency=provider_result.provider_cost_currency,
                duration_seconds=provider_result.duration_seconds,
            )

        return GenerationResult(
            generation=generation,
            model=model,
            result=provider_result.result,
            balance_after=balance_after,
        )

    def list_recent(self, *, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = self.generations.list_recent_for_user(
                conn, user_id=user_id, limit=limit
            )
            for row in rows:
                row["prompt_payload"] = loads_dict(row.get("prompt"))
                row["result_payload"] = loads_dict(row.get("result"))
            return rows


def _result_for_storage(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("kind") != "image" or "image_b64" not in result:
        return result
    stored = dict(result)
    stored.pop("image_b64", None)
    stored["image_data_saved"] = False
    return stored


def _generation_coin_cost(model: Dict[str, Any], prompt_text: str) -> int:
    if _is_image_four_k_request(model, prompt_text):
        config = loads_dict(model.get("config"))
        return max(1, int(config.get("four_k_coins_cost") or 3))
    return max(1, int(model["coins_cost"]))


def _is_image_four_k_request(model: Dict[str, Any], prompt_text: str) -> bool:
    if str(model.get("generation_type") or "") != "image":
        return False
    normalized = prompt_text.casefold()
    return "4k" in normalized or "4к" in normalized


def _provider_error_message(*, provider_error: str, generation_type: str) -> str:
    normalized = provider_error.casefold()
    suffix = "Монеты возвращены."
    if generation_type == "image":
        if "openai_image_api_key" in normalized or "not configured" in normalized:
            return (
                "Генерация фото сейчас не настроена: не задан ключ OpenAI Image. "
                f"{suffix}"
            )
        if "http 401" in normalized:
            return (
                "OpenAI не принял API-ключ для генерации фото. "
                f"{suffix}"
            )
        if (
            "http 403" in normalized
            or "organization verification" in normalized
            or "verify" in normalized
        ):
            return (
                "OpenAI не дал доступ к GPT Image для этого аккаунта. "
                "Проверьте доступ к модели и Organization Verification. "
                f"{suffix}"
            )
        if "http 400" in normalized and ("model" in normalized or "parameter" in normalized):
            return (
                "OpenAI не принял модель или параметры GPT Image. "
                f"{suffix}"
            )
        if "openai image api" in normalized:
            return f"OpenAI Image сейчас не смог создать фото. {suffix}"
    return f"Не получилось выполнить генерацию. {suffix}"
