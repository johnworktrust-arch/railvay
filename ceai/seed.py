from __future__ import annotations

from ceai.config import load_settings
from ceai.database import Database
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.plans import PlanRepository
from ceai.repositories.vpn_plans import VpnPlanRepository
from ceai.repositories.vpn_servers import VpnServerRepository


PLANS = [
    {
        "code": "start",
        "name": "Старт",
        "price_rub": 299,
        "duration_days": 30,
        "coins_amount": 25,
        "features": {
            "description": "Для знакомства с Cea AI.",
        },
    },
    {
        "code": "basic",
        "name": "Базовый",
        "price_rub": 699,
        "duration_days": 30,
        "coins_amount": 60,
        "features": {
            "description": "Для регулярного использования.",
        },
    },
    {
        "code": "pro",
        "name": "Про",
        "price_rub": 1490,
        "duration_days": 30,
        "coins_amount": 130,
        "features": {
            "description": "Для активной работы с AI-инструментами.",
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
            "max_input_characters": 6000,
            "max_output_tokens": 2000,
            "fallback_cost_usd": 0.001,
            "input_cost_per_million_usd": 0.14,
            "cached_input_cost_per_million_usd": 0.0028,
            "output_cost_per_million_usd": 0.28,
            "ui_description": (
                "Быстрая и экономная нейросетка для повседневных вопросов, "
                "идей, объяснений и черновиков."
            ),
        },
    },
    {
        "provider": "openai",
        "model_key": "gpt-4o-mini",
        "display_name": "ChatGPT GPT-5.6",
        "generation_type": "text",
        "coins_cost": 3,
        "config": {
            "api_model": "gpt-5.5",
            "reasoning_effort": "low",
            "max_input_characters": 6000,
            "max_output_tokens": 1500,
            "fallback_cost_usd": 0.06,
            "input_cost_per_million_usd": 5.0,
            "cached_input_cost_per_million_usd": 0.5,
            "output_cost_per_million_usd": 30.0,
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
        "coins_cost": 3,
        "config": {
            "api_model": "gpt-image-2",
            "quality": "medium",
            "size": "1024x1024",
            "output_format": "png",
            "fallback_cost_usd": 0.053,
            "text_input_cost_per_million_usd": 5.0,
            "image_input_cost_per_million_usd": 8.0,
            "image_output_cost_per_million_usd": 30.0,
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
        "coins_cost": 25,
        "config": {
            "api_model": "kling-v3",
            "mode": "std",
            "sound": "off",
            "aspect_ratio": "16:9",
            "resource_unit_cost_usd": 0.098,
            "resource_units_per_second": 0.6,
            "duration_seconds": 10,
            "ui_description": (
                "Создаёт короткие AI-видео по вашему описанию для роликов, "
                "идей и визуальных сцен."
            ),
        },
    },
    {
        "provider": "openai",
        "model_key": "tts-1",
        "display_name": "OpenAI TTS",
        "generation_type": "tts",
        "coins_cost": 3,
        "config": {
            "api_model": "tts-1",
            "voice": "alloy",
            "response_format": "mp3",
            "cost_per_million_characters_usd": 15.0,
            "duration_seconds": 15,
            "ui_description": (
                "Превращает текст в озвучку: удобно для роликов, сообщений "
                "и быстрых голосовых заготовок."
            ),
        },
    },
]

VPN_PLANS = [
    ("vpn-1m", "1 месяц", 30, 189, 149),
    ("vpn-3m", "3 месяца", 90, 479, 399),
    ("vpn-6m", "6 месяцев", 180, 790, 649),
    ("vpn-12m", "1 год", 365, 1290, 999),
]

VPN_ADDITIONAL_SERVERS = (
    {
        "code": "us-1",
        "name": "CEA VPN Charlotte 1",
        "provider": "marzban",
        "region": "US",
        "api_base_url": "http://127.0.0.1:8000",
        "worker_id": "cea-vpn-us",
        "subscription_base_url": "https://sub.77-110-119-28.sslip.io:8443",
    },
    {
        "code": "fi-1",
        "name": "CEA VPN Helsinki 1",
        "provider": "marzban",
        "region": "FI",
        "api_base_url": "http://127.0.0.1:8000",
        "worker_id": "cea-vpn-fi",
        "subscription_base_url": "https://sub.138-124-59-14.sslip.io:8443",
    },
)


def seed_reference_data(db: Database) -> None:
    settings = load_settings()
    plan_repo = PlanRepository()
    model_repo = ModelPriceRepository()
    vpn_plan_repo = VpnPlanRepository()
    vpn_server_repo = VpnServerRepository()
    with db.transaction() as conn:
        for plan in PLANS:
            plan_repo.upsert(conn, **plan)
        for model in MODEL_PRICES:
            model_repo.upsert(conn, **model)
        for code, name, duration_days, price_rub, price_stars in VPN_PLANS:
            vpn_plan_repo.upsert(
                conn,
                code=code,
                name=name,
                duration_days=duration_days,
                price_rub=price_rub,
                price_stars=price_stars,
                max_devices=1,
            )
        vpn_server_repo.upsert(
            conn,
            code=settings.vpn_server_code,
            name="CEA VPN Amsterdam 1",
            provider="marzban",
            region="NL",
            api_base_url="http://127.0.0.1:8000",
            worker_id=settings.vpn_worker_id,
            subscription_base_url=settings.vpn_subscription_base_url,
        )
        for server in VPN_ADDITIONAL_SERVERS:
            vpn_server_repo.upsert(conn, **server)
        conn.execute(
            """
            UPDATE model_prices
            SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
            WHERE generation_type = 'tts'
              AND NOT (provider = 'openai' AND model_key = 'tts-1')
            """
        )


def main() -> None:
    settings = load_settings()
    db = Database(settings.database_url)
    db.migrate()
    seed_reference_data(db)
    db.close()
    print("Database migrated and seeded.")


if __name__ == "__main__":
    main()
