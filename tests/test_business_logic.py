from __future__ import annotations

import sqlite3
import unittest
from json import loads
from pathlib import Path
from unittest.mock import patch

from ceai.config import Settings
from ceai.database import Database
from ceai.internal_api import handle_provider_settings_request
from ceai.json_utils import dumps, loads_dict
from ceai.repositories.app_settings import AppSettingsRepository
from ceai.providers.base import ProviderError
from ceai.providers.router import AIProviderRouter
from ceai.seed import seed_reference_data
from ceai.services.app import build_services
from ceai.services.exceptions import (
    BusinessRuleError,
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
)
from ceai.time_utils import iso_now


class BusinessLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        seed_reference_data(self.db)
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
        )
        self.services = build_services(self.db, settings)
        self.user = self.services.users.ensure_telegram_user(
            telegram_id=1001,
            username="tester",
            first_name="Test",
            last_name="User",
            language_code="ru",
        )

    def tearDown(self) -> None:
        self.db.close()

    def _buy_plan(self, plan_code: str = "start"):
        payment = self.services.payments.create_mock_payment(
            user_id=self.user["id"], plan_code=plan_code
        )
        return (
            payment,
            self.services.payments.process_mock_success_webhook_for_payment_id(
                payment_id=payment["id"]
            ),
        )

    def _model(self, model_key: str):
        for model in self.services.catalog.list_models():
            if model["model_key"] == model_key:
                return model
        raise AssertionError(f"Model {model_key} not found")

    def test_successful_mock_payment_credits_coins_once(self) -> None:
        payment, first = self._buy_plan("start")
        self.assertTrue(first.processed)
        self.assertEqual(first.credited_coins, 100)
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 100)

        second = self.services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        self.assertFalse(second.processed)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 100)

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount
                FROM coin_transactions
                WHERE payment_id = ? AND type = 'credit'
                """,
                (payment["id"],),
            ).fetchone()
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["amount"], 100)

    def test_generation_charges_coins_after_success(self) -> None:
        self._buy_plan("start")
        model = self._model("deepseek-v4-flash")

        result = self.services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Сделай краткий план",
        )

        self.assertEqual(result.generation["status"], "completed")
        self.assertEqual(result.generation["coins_reserved"], 1)
        self.assertEqual(result.generation["coins_charged"], 1)
        self.assertEqual(result.balance_after, 99)
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 99)

    def test_failed_generation_refunds_reserved_coins(self) -> None:
        self._buy_plan("start")
        model = self._model("deepseek-v4-flash")

        with self.assertRaises(GenerationProviderFailedError):
            self.services.generations.generate(
                user_id=self.user["id"],
                model_price_id=model["id"],
                prompt_text="mock_error",
            )

        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 100)
        history = self.services.generations.list_recent(user_id=self.user["id"])
        self.assertEqual(history[0]["status"], "failed")

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS amount
                FROM coin_transactions
                WHERE generation_id = ?
                """,
                (history[0]["id"],),
            ).fetchone()
        self.assertEqual(row["amount"], 0)

    def test_cannot_generate_without_active_subscription(self) -> None:
        model = self._model("deepseek-v4-flash")

        with self.assertRaises(NoActiveSubscriptionError):
            self.services.generations.generate(
                user_id=self.user["id"],
                model_price_id=model["id"],
                prompt_text="Привет",
            )

        history = self.services.generations.list_recent(user_id=self.user["id"])
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 0)

    def test_cannot_generate_when_coins_are_insufficient(self) -> None:
        self._buy_plan("start")
        model = self._model("kling-3")

        for index in range(4):
            self.services.generations.generate(
                user_id=self.user["id"],
                model_price_id=model["id"],
                prompt_text=f"Видео {index}",
            )
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 0)

        with self.assertRaises(InsufficientCoinsError):
            self.services.generations.generate(
                user_id=self.user["id"],
                model_price_id=model["id"],
                prompt_text="Еще одно видео",
            )

        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 0)


class MigrationAndUITest(unittest.TestCase):
    def test_reply_keyboard_has_no_audio_button_but_has_tts_button(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")

        self.assertNotIn("Аудио", keyboard_source)
        self.assertIn("Озвучка", keyboard_source)

    def test_reply_keyboard_uses_correct_deepseek_name(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")

        self.assertIn("ChatGPT", keyboard_source)
        self.assertIn("DeepSeek", keyboard_source)
        self.assertNotIn("DeepSeq", keyboard_source)
        self.assertNotIn("DeepSeak", keyboard_source)

    def test_ai_inner_keyboards_have_back_button_only(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn('BACK_TO_MENU_BUTTON = "⬅️ В меню"', keyboard_source)
        self.assertIn("def model_choice_keyboard(", keyboard_source)
        self.assertIn("model_choice_label(model)", keyboard_source)
        self.assertIn("state=\"waiting_model_choice\"", handlers_source)
        self.assertIn("reply_markup=model_choice_keyboard(models)", handlers_source)
        self.assertIn("reply_markup=back_to_menu_keyboard()", handlers_source)
        self.assertIn('"Запускаю генерацию..."', handlers_source)
        self.assertNotIn('"Запускаю mock-генерацию..."', handlers_source)

    def test_telegram_commands_menu_contains_only_menu_and_profile(self) -> None:
        main_source = Path("ceai/main.py").read_text(encoding="utf-8")

        self.assertIn('BotCommand(command="menu", description="Главное меню")', main_source)
        self.assertIn('BotCommand(command="profile", description="Профиль")', main_source)
        self.assertNotIn('BotCommand(command="admin"', main_source)
        self.assertNotIn('BotCommand(command="help"', main_source)

    def test_admin_command_is_hidden_and_silent_for_regular_users(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn('@router.message(Command("admin"))', handlers_source)
        self.assertNotIn("Доступ запрещен", handlers_source)

    def test_menu_command_has_main_menu_copy(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("🏠 Главное меню", handlers_source)
        self.assertIn("Выберите нужный раздел 👇", handlers_source)
        self.assertIn("Command(\"menu\")", handlers_source)

    def test_start_onboarding_copy_and_continue_callback_are_present(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("Приветствую в Cea AI", handlers_source)
        self.assertIn("Документ оферты здесь", handlers_source)
        self.assertIn("Чтобы узнать больше о своём аккаунте и тарифах", handlers_source)
        self.assertIn("В двух словах об основных инструментах", handlers_source)
        self.assertIn('F.data == "onboarding:continue"', handlers_source)
        self.assertIn("last_bot_message_ids", handlers_source)
        self.assertNotIn(
            "Добро пожаловать в CeaAI MVP. Здесь все AI и платежи",
            handlers_source,
        )

    def test_onboarding_keyboards_have_expected_buttons_only(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")

        self.assertIn("Продолжить", keyboard_source)
        self.assertIn("onboarding:continue", keyboard_source)
        self.assertIn("Все ай ай инфо", keyboard_source)
        self.assertIn("Поддержка", keyboard_source)
        self.assertIn("https://t.me/{username}", keyboard_source)
        self.assertNotIn("Как пользоваться", keyboard_source)
        self.assertNotIn("База знаний", keyboard_source)
        self.assertNotIn("VK", keyboard_source)
        self.assertNotIn("YT", keyboard_source)

    def test_onboarding_env_settings_are_read(self) -> None:
        from ceai.config import load_settings

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test",
                "PUBLIC_OFFER_URL": "https://cea.ai/offer",
                "INFO_CHANNEL_URL": "https://t.me/cea_ai_info",
                "SUPPORT_USERNAME": "@cea_help",
            },
        ):
            settings = load_settings()

        self.assertEqual(settings.public_offer_url, "https://cea.ai/offer")
        self.assertEqual(settings.info_channel_url, "https://t.me/cea_ai_info")
        self.assertEqual(settings.support_username, "cea_help")

    def test_ai_provider_env_settings_are_read(self) -> None:
        from ceai.config import load_settings

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test",
                "AI_PROVIDER_MODE": "real",
                "AI_REQUEST_TIMEOUT_SECONDS": "45",
                "DEEPSEEK_API_KEY": "deepseek-test",
                "DEEPSEEK_BASE_URL": "https://deepseek.test",
                "OPENAI_API_KEY": "openai-test",
                "OPENAI_BASE_URL": "https://openai.test/v1",
            },
        ):
            settings = load_settings()

        self.assertEqual(settings.ai_provider_mode, "real")
        self.assertEqual(settings.ai_request_timeout_seconds, 45)
        self.assertEqual(settings.deepseek_api_key, "deepseek-test")
        self.assertEqual(settings.deepseek_base_url, "https://deepseek.test")
        self.assertEqual(settings.openai_api_key, "openai-test")
        self.assertEqual(settings.openai_base_url, "https://openai.test/v1")

    def test_seed_text_models_are_configured_for_real_api(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            seed_reference_data(db)
            with db.transaction() as conn:
                deepseek = conn.execute(
                    "SELECT * FROM model_prices WHERE provider = ? AND model_key = ?",
                    ("deepseek", "deepseek-v4-flash"),
                ).fetchone()
                openai = conn.execute(
                    "SELECT * FROM model_prices WHERE provider = ? AND model_key = ?",
                    ("openai", "gpt-4o-mini"),
                ).fetchone()

            self.assertEqual(
                loads_dict(deepseek["config"])["api_model"], "deepseek-v4-flash"
            )
            self.assertEqual(
                loads_dict(deepseek["config"])["thinking_type"], "disabled"
            )
            self.assertEqual(openai["display_name"], "ChatGPT GPT-5.5")
            self.assertEqual(loads_dict(openai["config"])["api_model"], "gpt-5.5")
            self.assertEqual(loads_dict(openai["config"])["reasoning_effort"], "low")
        finally:
            db.close()

    def test_ai_provider_router_requires_real_provider_in_real_mode(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            ai_provider_mode="real",
        )
        router = AIProviderRouter(settings)

        with self.assertRaises(ProviderError):
            router.generate(
                model={
                    "provider": "deepseek",
                    "model_key": "deepseek-v4-flash",
                    "display_name": "DeepSeek V4 Flash",
                    "generation_type": "text",
                    "config": "{}",
                },
                prompt_text="Привет",
            )

    def test_ai_provider_router_uses_saved_provider_keys(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            with db.transaction() as conn:
                repo = AppSettingsRepository()
                repo.upsert(
                    conn,
                    key="DEEPSEEK_API_KEY",
                    value="saved-deepseek-key",
                    is_secret=True,
                )
                repo.upsert(
                    conn,
                    key="OPENAI_API_KEY",
                    value="saved-openai-key",
                    is_secret=True,
                )

            settings = Settings(
                telegram_bot_token="test",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
                ai_provider_mode="auto",
            )
            router = AIProviderRouter(settings, db)

            self.assertIsNotNone(router.deepseek)
            self.assertIsNotNone(router.openai)
        finally:
            db.close()

    def test_ai_provider_router_reloads_saved_provider_keys(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            settings = Settings(
                telegram_bot_token="test",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
                ai_provider_mode="auto",
            )
            router = AIProviderRouter(settings, db)
            self.assertIsNone(router.deepseek)
            self.assertIsNone(router.openai)

            with db.transaction() as conn:
                repo = AppSettingsRepository()
                repo.upsert(
                    conn,
                    key="DEEPSEEK_API_KEY",
                    value="saved-deepseek-key",
                    is_secret=True,
                )
                repo.upsert(
                    conn,
                    key="OPENAI_API_KEY",
                    value="saved-openai-key",
                    is_secret=True,
                )
            router.reload_settings()

            self.assertIsNotNone(router.deepseek)
            self.assertIsNotNone(router.openai)
        finally:
            db.close()

    def test_internal_provider_settings_endpoint_saves_keys(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            settings = Settings(
                telegram_bot_token="secret-token",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
            )
            status, content_type, body = handle_provider_settings_request(
                settings=settings,
                db=db,
                headers={"Authorization": "Bearer secret-token"},
                body=dumps(
                    {
                        "settings": {
                            "AI_PROVIDER_MODE": "auto",
                            "DEEPSEEK_API_KEY": "deepseek-test",
                            "OPENAI_API_KEY": "openai-test",
                        }
                    }
                ).encode("utf-8"),
            )

            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertTrue(loads(body)["ok"])
            with db.transaction() as conn:
                saved = AppSettingsRepository().get_many(
                    conn, ("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
                )
            self.assertEqual(saved["DEEPSEEK_API_KEY"], "deepseek-test")
            self.assertEqual(saved["OPENAI_API_KEY"], "openai-test")
        finally:
            db.close()

    def test_internal_provider_settings_endpoint_requires_auth(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            settings = Settings(
                telegram_bot_token="secret-token",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
            )
            status, _, body = handle_provider_settings_request(
                settings=settings,
                db=db,
                headers={},
                body=dumps({"settings": {"AI_PROVIDER_MODE": "auto"}}).encode("utf-8"),
            )

            self.assertEqual(status, 401)
            self.assertFalse(loads(body)["ok"])
        finally:
            db.close()

    def test_admin_user_card_formats_dates_without_iso_noise(self) -> None:
        from ceai.formatting import format_datetime_minute

        registered_at = format_datetime_minute("2026-06-20T11:22:33.123456+00:00")
        last_seen_at = format_datetime_minute("2026-06-20T11:25:59+00:00")

        self.assertEqual(registered_at, "20.06.2026 14:22")
        self.assertEqual(last_seen_at, "20.06.2026 14:25")
        self.assertNotIn("T11:22", registered_at)
        self.assertNotIn("+00:00", last_seen_at)

    def test_migrations_record_applied_versions_once(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            db.migrate()
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM schema_migrations"
                ).fetchone()
            self.assertEqual(row["count"], 4)
        finally:
            db.close()

    def test_music_generation_type_is_rejected_after_migrations(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            now = iso_now()
            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO model_prices (
                            provider, model_key, display_name, generation_type,
                            coins_cost, is_active, config, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, 1, '{}', ?, ?)
                        """,
                        (
                            "mock",
                            "music-test",
                            "Music Test",
                            "music",
                            1,
                            now,
                            now,
                        ),
                    )

            with db.transaction() as conn:
                user_id = conn.execute(
                    """
                    INSERT INTO users (
                        telegram_id, referral_code, created_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (4242, "tg4242", now, now),
                ).lastrowid
                model_id = conn.execute(
                    """
                    INSERT INTO model_prices (
                        provider, model_key, display_name, generation_type,
                        coins_cost, is_active, config, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, '{}', ?, ?)
                    """,
                    (
                        "mock",
                        "text-test",
                        "Text Test",
                        "text",
                        1,
                        now,
                        now,
                    ),
                ).lastrowid

            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO generations (
                            user_id, model_price_id, generation_type, provider,
                            status, prompt, created_at
                        )
                        VALUES (?, ?, ?, ?, 'pending', '{}', ?)
                        """,
                        (user_id, model_id, "music", "mock", now),
                    )
        finally:
            db.close()


class AdminLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        seed_reference_data(self.db)
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            admin_telegram_ids=(9001,),
            admin_telegram_usernames=("samescam",),
        )
        self.services = build_services(self.db, settings)
        self.admin_user = self.services.users.ensure_telegram_user(
            telegram_id=9001,
            username="owner",
            first_name="Owner",
            language_code="ru",
        )
        self.admin = self.services.admin.ensure_admin_access(self.admin_user)

    def tearDown(self) -> None:
        self.db.close()

    def _user(self, telegram_id: int, username: str | None = None):
        return self.services.users.ensure_telegram_user(
            telegram_id=telegram_id,
            username=username,
            first_name=f"User {telegram_id}",
            language_code="ru",
        )

    def _buy_plan(self, user_id: int, plan_code: str = "start"):
        payment = self.services.payments.create_mock_payment(
            user_id=user_id, plan_code=plan_code
        )
        self.services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        return payment

    def _model(self, model_key: str):
        for model in self.services.catalog.list_models():
            if model["model_key"] == model_key:
                return model
        raise AssertionError(f"Model {model_key} not found")

    def test_env_admin_and_samescam_username_get_admin_access(self) -> None:
        self.assertIsNotNone(self.admin)
        self.assertEqual(self.admin["role"], "owner")

        username_admin = self._user(9002, "samescam")
        admin = self.services.admin.ensure_admin_access(username_admin)
        self.assertIsNotNone(admin)
        self.assertEqual(admin["role"], "owner")

    def test_regular_user_is_denied_admin_access(self) -> None:
        regular = self._user(9003, "regular")

        self.assertIsNone(self.services.admin.ensure_admin_access(regular))

    def test_users_list_contains_profiles_and_paginates(self) -> None:
        for index in range(12):
            self._user(9100 + index, f"user{index}")

        first_page = self.services.admin.list_users(page=1)
        second_page = self.services.admin.list_users(page=2)

        self.assertEqual(first_page["page"], 1)
        self.assertGreaterEqual(first_page["pages"], 2)
        self.assertEqual(len(first_page["users"]), 10)
        self.assertGreaterEqual(len(second_page["users"]), 3)
        self.assertIn("telegram_id", first_page["users"][0])
        self.assertIn("username", first_page["users"][0])

    def test_user_card_counts_balance_payments_generations_and_spent_coins(self) -> None:
        target = self._user(9201, "target")
        self._buy_plan(target["id"])
        model = self._model("deepseek-v4-flash")
        self.services.generations.generate(
            user_id=target["id"],
            model_price_id=model["id"],
            prompt_text="Привет",
        )

        card = self.services.admin.user_card(target["id"])

        self.assertEqual(card["subscription"]["coins_balance_cache"], 99)
        self.assertEqual(card["payments"]["paid_count"], 1)
        self.assertEqual(card["payments"]["paid_amount_rub"], 299)
        self.assertEqual(card["generations"]["total"], 1)
        self.assertEqual(card["generations"]["spent_coins"], 1)

    def test_ban_and_unban_toggle_regular_user_access(self) -> None:
        target = self._user(9301, "blocked")

        self.services.admin.set_blocked(
            admin=self.admin, target_user_id=target["id"], is_blocked=True
        )
        blocked = self.services.users.ensure_telegram_user(
            telegram_id=9301, username="blocked"
        )
        self.assertTrue(self.services.admin.is_blocked_regular_user(blocked))

        self.services.admin.set_blocked(
            admin=self.admin, target_user_id=target["id"], is_blocked=False
        )
        unblocked = self.services.users.ensure_telegram_user(
            telegram_id=9301, username="blocked"
        )
        self.assertFalse(self.services.admin.is_blocked_regular_user(unblocked))

    def test_manual_credit_updates_balance_transaction_and_audit_log(self) -> None:
        target = self._user(9401, "credited")
        self._buy_plan(target["id"])

        balance = self.services.admin.manual_credit(
            admin=self.admin, target_user_id=target["id"], amount=25
        )

        self.assertEqual(balance, 125)
        with self.db.transaction() as conn:
            transaction = conn.execute(
                """
                SELECT * FROM coin_transactions
                WHERE user_id = ? AND type = 'manual_adjustment'
                """,
                (target["id"],),
            ).fetchone()
            log = conn.execute(
                """
                SELECT * FROM admin_action_logs
                WHERE admin_user_id = ? AND target_user_id = ?
                    AND action = 'manual_credit'
                """,
                (self.admin["user_id"], target["id"]),
            ).fetchone()

        self.assertIsNotNone(transaction)
        self.assertEqual(transaction["amount"], 25)
        self.assertEqual(transaction["reason"], "admin_manual_credit")
        self.assertIsNotNone(log)

    def test_manual_credit_requires_active_subscription(self) -> None:
        target = self._user(9501, "no_subscription")

        with self.assertRaises(BusinessRuleError):
            self.services.admin.manual_credit(
                admin=self.admin, target_user_id=target["id"], amount=10
            )


if __name__ == "__main__":
    unittest.main()
