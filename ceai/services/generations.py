from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from ceai.database import Database
from ceai.json_utils import loads_dict
from ceai.providers.mock import MockAIProvider, MockProviderError
from ceai.repositories.generations import GenerationRepository
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.subscriptions import SubscriptionRepository
from ceai.services.coins import CoinService
from ceai.services.exceptions import (
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)


@dataclass(frozen=True)
class GenerationResult:
    generation: Dict[str, Any]
    model: Dict[str, Any]
    result: Dict[str, Any]
    balance_after: int


class GenerationService:
    def __init__(self, db: Database, provider: MockAIProvider) -> None:
        self.db = db
        self.provider = provider
        self.models = ModelPriceRepository()
        self.subscriptions = SubscriptionRepository()
        self.generations = GenerationRepository()
        self.coins = CoinService()

    def generate(
        self, *, user_id: int, model_price_id: int, prompt_text: str
    ) -> GenerationResult:
        business_error: NoActiveSubscriptionError | InsufficientCoinsError | None = None
        with self.db.transaction() as conn:
            model = self.models.get_by_id(conn, model_price_id)
            if model is None or not model["is_active"]:
                raise NotFoundError("Модель не найдена")

            generation = self.generations.create_pending(
                conn,
                user_id=user_id,
                model_price_id=model["id"],
                generation_type=model["generation_type"],
                provider=model["provider"],
                prompt={"text": prompt_text},
            )
            subscription = self.subscriptions.get_active_for_user(conn, user_id)
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
                cost = int(model["coins_cost"])
                if balance < cost:
                    self.generations.mark_failed(
                        conn,
                        generation_id=generation["id"],
                        subscription_id=subscription["id"],
                        error_message="Insufficient coins",
                    )
                    business_error = InsufficientCoinsError(
                        "Недостаточно coins", generation_id=generation["id"]
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
            provider_result = self.provider.generate(model=model, prompt_text=prompt_text)
        except MockProviderError as exc:
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
                "AI provider error, coins refunded",
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
                result=provider_result.result,
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
