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
            "description": "Для знакомства с DeepSeek, ChatGPT и GPT Image",
            "video_limit": 0,
        },
    },
    {
        "code": "basic",
        "name": "Базовый",
        "price_rub": 699,
        "duration_days": 30,
        "coins_amount": 230,
        "features": {
            "description": "Для регулярной работы с текстом и изображениями",
            "video_limit": 0,
        },
    },
    {
        "code": "pro",
        "name": "Про",
        "price_rub": 1490,
        "duration_days": 30,
        "coins_amount": 500,
        "features": {
            "description": "Для активной работы с ChatGPT, DeepSeek и GPT Image",
            "video_limit": 0,
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
        "config": {
            "api_model": "deepseek-v4-flash",
            "thinking_type": "disabled",
            "provider_cost_amount": 0.05,
            "provider_cost_currency": "RUB",
            "ui_description": (
                "Быстрая и экономная нейросетка для повседневных вопросов, "
                "идей, объяснений и черновиков."
            ),
        },
    },
    {
        "provider": "openai",
        "model_key": "gpt-4o-mini",
        "display_name": "ChatGPT GPT-5.5",
        "generation_type": "text",
        "coins_cost": 3,
        "config": {
            "api_model": "gpt-5.5",
            "reasoning_effort": "low",
            "provider_cost_amount": 7.5,
            "provider_cost_currency": "RUB",
            "ui_description": (
                "Сильная универсальная модель для сложных запросов, текстов, "
                "аналитики и аккуратных ответов."
            ),
        },
    },
    {
        "provider": "openai",
        "model_key": "gpt-image-2-medium",
        "display_name": "GPT Image 2",
        "generation_type": "image",
        "coins_cost": 2,
        "config": {
            "api_model": "gpt-image-2",
            "quality": "medium",
            "size": "1024x1024",
            "output_format": "png",
            "four_k_coins_cost": 3,
            "provider_cost_amount": 4.5,
            "provider_cost_currency": "RUB",
            "ui_description": (
                "Генерирует изображения по описанию: от быстрых визуальных "
                "идей до готовых иллюстраций."
            ),
        },
    },
    {
        "provider": "kling",
        "model_key": "kling-3",
        "display_name": "Kling 3.0",
        "generation_type": "video",
        "coins_cost": 35,
        "config": {
            "api_model": "kling-v3",
            "mode": "std",
            "sound": "off",
            "aspect_ratio": "16:9",
            "provider_cost_amount": 65,
            "provider_cost_currency": "RUB",
            "duration_seconds": 10,
            "ui_description": (
                "Создаёт короткие AI-видео по вашему описанию для роликов, "
                "идей и визуальных сцен."
            ),
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
            "ui_description": (
                "Превращает текст в озвучку: удобно для роликов, сообщений "
                "и быстрых голосовых заготовок."
            ),
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
