from __future__ import annotations

from ceai.config import load_settings
from ceai.database import Database
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.plans import PlanRepository


PLANS = [
    {
        "code": "start",
        "name": "Старт",
        "price_rub": 299,
        "duration_days": 30,
        "coins_amount": 100,
        "features": {
            "description": "Для первого знакомства с CeaAI",
            "video_limit": 0,
        },
    },
    {
        "code": "basic",
        "name": "Базовый",
        "price_rub": 699,
        "duration_days": 30,
        "coins_amount": 300,
        "features": {
            "description": "Для регулярной работы с текстом, картинками и озвучкой",
            "video_limit": 1,
        },
    },
    {
        "code": "pro",
        "name": "Про",
        "price_rub": 1490,
        "duration_days": 30,
        "coins_amount": 800,
        "features": {
            "description": "Для активной генерации контента",
            "video_limit": 5,
        },
    },
]

MODEL_PRICES = [
    {
        "provider": "deepseek",
        "model_key": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash",
        "generation_type": "text",
        "coins_cost": 1,
        "config": {"provider_cost_amount": 0.03, "provider_cost_currency": "RUB"},
    },
    {
        "provider": "openai",
        "model_key": "gpt-4o-mini",
        "display_name": "GPT-4o mini",
        "generation_type": "text",
        "coins_cost": 2,
        "config": {"provider_cost_amount": 0.2, "provider_cost_currency": "RUB"},
    },
    {
        "provider": "openai",
        "model_key": "gpt-image-2-medium",
        "display_name": "GPT Image 2 Medium",
        "generation_type": "image",
        "coins_cost": 6,
        "config": {"provider_cost_amount": 4.5, "provider_cost_currency": "RUB"},
    },
    {
        "provider": "kling",
        "model_key": "kling-3",
        "display_name": "Kling 3.0",
        "generation_type": "video",
        "coins_cost": 25,
        "config": {
            "provider_cost_amount": 92,
            "provider_cost_currency": "RUB",
            "duration_seconds": 10,
        },
    },
    {
        "provider": "elevenlabs",
        "model_key": "elevenlabs-tts",
        "display_name": "ElevenLabs TTS",
        "generation_type": "tts",
        "coins_cost": 5,
        "config": {
            "provider_cost_amount": 9,
            "provider_cost_currency": "RUB",
            "duration_seconds": 15,
        },
    },
]


def seed_reference_data(db: Database) -> None:
    plan_repo = PlanRepository()
    model_repo = ModelPriceRepository()
    with db.transaction() as conn:
        for plan in PLANS:
            plan_repo.upsert(conn, **plan)
        for model in MODEL_PRICES:
            model_repo.upsert(conn, **model)


def main() -> None:
    settings = load_settings()
    db = Database(settings.database_url)
    db.migrate()
    seed_reference_data(db)
    db.close()
    print("Database migrated and seeded.")


if __name__ == "__main__":
    main()
