from __future__ import annotations

import sqlite3
import unittest
from json import loads
from pathlib import Path
from unittest.mock import patch

from ceai.config import DEFAULT_INFO_CHANNEL_URL, DEFAULT_PUBLIC_OFFER_URL, Settings
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

    def test_start_referral_assigns_referrer_once_and_rejects_self(self) -> None:
        friend = self.services.users.ensure_telegram_user(
            telegram_id=2002,
            username="friend",
            first_name="Friend",
            language_code="ru",
        )
        other = self.services.users.ensure_telegram_user(
            telegram_id=3003,
            username="other",
            first_name="Other",
            language_code="ru",
        )

        assigned = self.services.referrals.apply_start_referral(
            user_id=friend["id"],
            start_text="/start ref_tg1001",
        )
        self.assertTrue(assigned)
        self.assertEqual(assigned.referrer_user_id, self.user["id"])
        self.assertEqual(assigned.referrer_telegram_id, 1001)
        self.assertEqual(assigned.referred_user_id, friend["id"])
        self.assertEqual(assigned.referred_telegram_id, 2002)
        self.assertFalse(
            self.services.referrals.apply_start_referral(
                user_id=friend["id"],
                start_text="/start ref_tg3003",
            )
        )
        already_registered = self.services.referrals.apply_start_referral(
            user_id=other["id"],
            start_text="/start ref_tg1001",
            user_was_registered=True,
        )
        self.assertFalse(already_registered)
        self.assertTrue(already_registered.already_registered)
        self.assertFalse(
            self.services.referrals.apply_start_referral(
                user_id=self.user["id"],
                start_text="/start ref_tg1001",
            )
        )

        with self.db.transaction() as conn:
            friend_row = conn.execute(
                "SELECT referred_by_user_id FROM users WHERE id = ?",
                (friend["id"],),
            ).fetchone()
            other_row = conn.execute(
                "SELECT referred_by_user_id FROM users WHERE id = ?",
                (other["id"],),
            ).fetchone()

        self.assertEqual(friend_row["referred_by_user_id"], self.user["id"])
        self.assertIsNone(other_row["referred_by_user_id"])
        self.assertEqual(self.services.referrals.stats(self.user["id"]).invited_count, 1)

    def test_referral_reward_from_paid_payment_is_credited_once(self) -> None:
        friend = self.services.users.ensure_telegram_user(
            telegram_id=2002,
            username="friend",
            first_name="Friend",
            language_code="ru",
        )
        self.assertTrue(
            self.services.referrals.apply_start_referral(
                user_id=friend["id"],
                start_text="/start ref_tg1001",
            )
        )

        payment = self.services.payments.create_mock_payment(
            user_id=friend["id"],
            plan_code="start",
        )
        first = self.services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        second = self.services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )

        self.assertTrue(first.processed)
        self.assertEqual(first.referral_reward_kopecks, 8970)
        self.assertFalse(second.processed)
        self.assertTrue(second.duplicate)

        stats = self.services.referrals.stats(self.user["id"])
        self.assertEqual(stats.invited_count, 1)
        self.assertEqual(stats.balance_kopecks, 8970)

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(amount_kopecks), 0) AS amount
                FROM referral_transactions
                WHERE payment_id = ? AND type = 'credit'
                """,
                (payment["id"],),
            ).fetchone()

        self.assertEqual(row["count"], 1)
        self.assertEqual(row["amount"], 8970)

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

    def test_text_chats_have_defaults_custom_delete_and_protect_defaults(self) -> None:
        model = self._model("deepseek-v4-flash")

        chats = self.services.text_chats.list_for_model(
            user_id=self.user["id"], model_price_id=model["id"]
        )
        self.assertEqual(
            [chat["title"] for chat in chats[:5]],
            ["Основной", "Медицина", "Работа", "Психолог", "Спорт"],
        )
        prompts = {chat["title"]: chat["system_prompt"] for chat in chats}
        self.assertIn("медицинским", prompts["Медицина"])
        self.assertIn("рабочих задач", prompts["Работа"])
        self.assertIn("психологический", prompts["Психолог"])
        self.assertIn("спорту", prompts["Спорт"])

        custom = self.services.text_chats.create_custom(
            user_id=self.user["id"],
            model_price_id=model["id"],
            title="Мой чат",
        )
        self.assertFalse(custom["is_default"])
        fallback = self.services.text_chats.delete(
            user_id=self.user["id"], chat_id=custom["id"]
        )
        self.assertEqual(fallback["title"], "Основной")

        with self.assertRaises(BusinessRuleError):
            self.services.text_chats.delete(user_id=self.user["id"], chat_id=chats[0]["id"])

    def test_text_generation_stores_selected_chat_in_prompt(self) -> None:
        self._buy_plan("start")
        model = self._model("deepseek-v4-flash")
        chat = self.services.text_chats.create_custom(
            user_id=self.user["id"],
            model_price_id=model["id"],
            title="Рабочие вопросы",
        )

        self.services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Сделай список задач",
            text_chat_id=chat["id"],
            text_chat_title=chat["title"],
            text_chat_system_prompt=chat["system_prompt"],
        )

        history = self.services.generations.list_recent(user_id=self.user["id"])
        self.assertEqual(history[0]["prompt_payload"]["text_chat_id"], chat["id"])
        self.assertEqual(history[0]["prompt_payload"]["text_chat_title"], "Рабочие вопросы")
        self.assertIn(
            "пользовательском чате",
            history[0]["prompt_payload"]["text_chat_system_prompt"],
        )

    def test_profile_counts_invited_users_and_links_account_name(self) -> None:
        from ceai.bot.handlers import _format_menu, _format_referral_screen

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, referral_code,
                    referred_by_user_id, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    2002,
                    "friend",
                    "Friend",
                    "tg2002",
                    self.user["id"],
                    iso_now(),
                    iso_now(),
                ),
            )

        invited_count = self.services.users.count_invited_users(self.user["id"])
        profile = _format_menu(
            self.user,
            None,
            invited_users_count=invited_count,
        )

        self.assertEqual(invited_count, 1)
        self.assertIn('👤 Профиль: <a href="tg://user?id=1001">@tester</a>', profile)
        self.assertIn("ℹ️ ID: 1001", profile)
        self.assertIn("💰 Баланс: 0 coins", profile)
        self.assertIn("⭐ Подписка: нет активной", profile)
        self.assertIn("📅 Срок действия: —", profile)
        self.assertIn(
            "⭐ Подписка: нет активной\n📅 Срок действия: —\n\n👥 Приглашено: 1",
            profile,
        )
        self.assertIn("👥 Приглашено: 1", profile)
        self.assertNotIn("зарабатывайте 30%", profile.casefold())

        active_profile = _format_menu(
            self.user,
            {
                "coins_balance_cache": 42,
                "plan_name": "Про",
                "ends_at": "2026-06-24T17:16:00+00:00",
            },
            invited_users_count=2,
        )
        self.assertIn("💰 Баланс: 42 coins", active_profile)
        self.assertIn("⭐ Подписка: Про", active_profile)
        self.assertIn("📅 Срок действия: 24 июня 2026 года, 20:16", active_profile)
        self.assertIn("👥 Приглашено: 2", active_profile)
        self.assertNotIn("Про до", active_profile)

        zero_invites_profile = _format_menu(
            {
                "id": 3003,
                "telegram_id": 3003,
                "username": None,
                "first_name": "No Username",
                "last_name": "",
            },
            None,
            invited_users_count=0,
        )
        self.assertIn(
            '<a href="tg://user?id=3003">No Username</a>',
            zero_invites_profile,
        )
        self.assertIn(
            "👥 Приглашено: 0 (Приглашайте друзей и зарабатывайте 30% с каждого пополнения!)",
            zero_invites_profile,
        )
        self.assertNotIn("Получайте 2", zero_invites_profile)

        referral = _format_referral_screen(self.user, invited_users_count=1)
        self.assertIn("<blockquote>", referral)
        self.assertIn("</blockquote>", referral)
        self.assertIn(
            "Приглашайте друзей и зарабатывайте 30% с каждого пополнения!",
            referral,
        )
        self.assertIn("— Друзья перешли по вашей ссылке и потратили 1000₽", referral)
        self.assertIn("— Вы получаете 300.0₽ и выводите на КАРТУ!", referral)
        self.assertIn("— Приглашено: 1", referral)
        self.assertIn("— Баланс: 0 ₽", referral)
        self.assertIn("— Способ вывода: не задан", referral)
        self.assertIn("— Реквизиты: не указаны", referral)
        self.assertIn("% <b>Текущая ставка: 30%</b>", referral)
        self.assertIn("💼 Вывод доступен от 1000₽", referral)
        self.assertIn("📨 Нажмите на ссылку", referral)
        self.assertNotIn("🪁", referral)
        self.assertIn(
            "<code>https://t.me/aiceabot?start=ref_tg1001</code>",
            referral,
        )
        self.assertNotIn("USDT", referral.upper())

        referral_with_stats = _format_referral_screen(
            self.user,
            invited_users_count=2,
            balance_kopecks=44370,
            withdrawal_method="карта",
            requisites="**** 1234",
        )
        self.assertIn("— Приглашено: 2", referral_with_stats)
        self.assertIn("— Баланс: 443.70 ₽", referral_with_stats)
        self.assertIn("— Способ вывода: карта", referral_with_stats)
        self.assertIn("— Реквизиты: **** 1234", referral_with_stats)

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

    def test_ai_reply_keyboards_have_back_button_only(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        seed_source = Path("ceai/seed.py").read_text(encoding="utf-8")
        model_keyboard_source = keyboard_source.split(
            "def models_keyboard(", 1
        )[1].split("def model_choice_label", 1)[0]

        self.assertIn('BACK_TO_MENU_BUTTON = "⬅️ Назад"', keyboard_source)
        self.assertIn(
            "def models_keyboard(models: Iterable[Dict[str, Any]]) -> ReplyKeyboardMarkup",
            keyboard_source,
        )
        self.assertIn("KeyboardButton(text=model_choice_label(model))", model_keyboard_source)
        self.assertIn('input_field_placeholder="Выберите модель"', model_keyboard_source)
        self.assertNotIn("InlineKeyboardButton", model_keyboard_source)
        self.assertNotIn("callback_data", model_keyboard_source)
        self.assertIn("state=\"waiting_model_choice\"", handlers_source)
        self.assertIn("reply_markup=models_keyboard(models)", handlers_source)
        self.assertIn("ui_description", handlers_source)
        self.assertIn("ui_description", seed_source)
        self.assertIn("Стоимость: {model['coins_cost']} coins за запрос.", handlers_source)
        self.assertNotIn('lines = ["Выберите AI-инструмент:"]', handlers_source)
        self.assertIn("reply_markup=back_to_menu_keyboard()", handlers_source)
        self.assertIn("def back_to_menu_keyboard() -> ReplyKeyboardMarkup", keyboard_source)
        self.assertIn("KeyboardButton(text=BACK_TO_MENU_BUTTON)", keyboard_source)
        self.assertIn("Выберите модель на нижней клавиатуре.", handlers_source)
        self.assertIn('"Запускаю генерацию..."', handlers_source)
        self.assertIn("_format_generation_result(generation.result)", handlers_source)
        self.assertNotIn("Баланс после генерации", handlers_source)
        self.assertNotIn('"Запускаю mock-генерацию..."', handlers_source)

    def test_text_chat_navigation_has_back_and_no_premature_current_chat(
        self,
    ) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        chat_keyboard_source = keyboard_source.split(
            "def text_chat_keyboard(", 1
        )[1].split("def text_chat_prompt_keyboard", 1)[0]
        prompt_keyboard_source = keyboard_source.split(
            "def text_chat_prompt_keyboard(", 1
        )[1].split("def admin_menu_keyboard", 1)[0]

        self.assertIn("Основной", chat_keyboard_source)
        self.assertIn("ADD_TEXT_CHAT_BUTTON", chat_keyboard_source)
        self.assertNotIn("DELETE_CURRENT_TEXT_CHAT_BUTTON", chat_keyboard_source)
        self.assertIn("BACK_TO_MENU_BUTTON", chat_keyboard_source)
        self.assertIn("KeyboardButton(text=text_chat_label", chat_keyboard_source)
        self.assertIn("KeyboardButton(text=ADD_TEXT_CHAT_BUTTON)", chat_keyboard_source)
        self.assertIn("KeyboardButton(text=BACK_TO_MENU_BUTTON)", chat_keyboard_source)
        self.assertIn('input_field_placeholder="Выберите чат"', chat_keyboard_source)
        self.assertNotIn("InlineKeyboardButton", chat_keyboard_source)
        self.assertNotIn("callback_data", chat_keyboard_source)
        self.assertNotIn('TEXT_CHAT_LIST_BUTTON = "К чатам"', keyboard_source)
        self.assertNotIn('"К чатам"', keyboard_source)
        self.assertNotIn('"Текущий чат:', handlers_source)
        self.assertNotIn("Введите текст, что хотите спросить у нейросетки.", handlers_source)
        self.assertNotIn('prefix = "✓ "', keyboard_source)
        self.assertIn("def text_chat_keyboard(", keyboard_source)
        self.assertIn("def text_chat_prompt_keyboard(", keyboard_source)
        self.assertIn("def text_chat_prompt_keyboard() -> ReplyKeyboardMarkup", keyboard_source)
        self.assertIn("KeyboardButton(text=DELETE_CURRENT_TEXT_CHAT_BUTTON)", prompt_keyboard_source)
        self.assertIn("KeyboardButton(text=BACK_TO_MENU_BUTTON)", prompt_keyboard_source)
        self.assertIn('input_field_placeholder="Введите вопрос"', prompt_keyboard_source)
        self.assertNotIn("InlineKeyboardButton", prompt_keyboard_source)
        self.assertNotIn("callback_data", prompt_keyboard_source)
        self.assertIn("state=\"waiting_text_chat_choice\"", handlers_source)
        self.assertIn('if action == "back":', handlers_source)
        self.assertIn("current_text_chat_id\": int(current_chat[\"id\"]) if current_chat else 0", handlers_source)
        self.assertIn('F.data.startswith("text_chat:")', handlers_source)
        self.assertIn("Выберите чат на нижней клавиатуре.", handlers_source)
        self.assertIn("waiting_text_chat_prompt", handlers_source)
        self.assertIn("waiting_text_chat_name", handlers_source)
        self.assertIn("text_chat_id", handlers_source)
        self.assertIn("text_chat_system_prompt", handlers_source)

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

    def test_profile_screen_has_inline_actions_and_no_bottom_prompt(self) -> None:
        from ceai.bot.keyboards import profile_keyboard

        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        profile_format_source = handlers_source.split(
            "def _format_menu(", 1
        )[1].split("def _format_onboarding_greeting", 1)[0]
        send_profile_source = handlers_source.split(
            "async def _send_main_menu(", 1
        )[1].split("async def _send_onboarding_greeting", 1)[0]

        self.assertIn("def profile_keyboard(", keyboard_source)
        labels = [row[0].text for row in profile_keyboard().inline_keyboard]
        self.assertEqual(
            labels,
            [
                "💳 Подписка и тарифы",
                "🤝 Реферальная программа",
                "🆘 Поддержка",
                "⬅️ Назад",
            ],
        )
        self.assertIn("Подписка и тарифы", keyboard_source)
        self.assertNotIn("🏠 Главное меню", keyboard_source)
        self.assertIn("BACK_TO_MENU_BUTTON", keyboard_source)
        self.assertIn("Поддержка", keyboard_source)
        self.assertIn("Реферальная программа", keyboard_source)
        self.assertIn('callback_data="menu:referral"', keyboard_source)
        self.assertIn('callback_data="menu:main"', keyboard_source)
        self.assertIn("reply_markup=profile_keyboard()", send_profile_source)
        self.assertIn("services.users.get_by_id", send_profile_source)
        self.assertIn("services.referrals.stats", send_profile_source)
        self.assertIn('parse_mode="HTML"', send_profile_source)
        self.assertIn('href="tg://user?id={telegram_id}"', handlers_source)
        self.assertIn("👤 Профиль:", profile_format_source)
        self.assertIn("ℹ️ ID:", profile_format_source)
        self.assertIn("💰 Баланс:", profile_format_source)
        self.assertIn("📅 Срок действия:", profile_format_source)
        self.assertIn("format_datetime_russian_minute", handlers_source)
        self.assertIn("👥 Приглашено:", profile_format_source)
        self.assertIn("Приглашайте друзей и зарабатывайте 30% с каждого пополнения!", profile_format_source)
        self.assertNotIn("Приглашенные пользователи", profile_format_source)
        self.assertNotIn("Получайте 2", profile_format_source)
        self.assertIn("async def _send_screen_message", handlers_source)
        self.assertNotIn("await _remove_reply_keyboard", send_profile_source)
        self.assertNotIn("Выберите действие на нижней клавиатуре", profile_format_source)
        self.assertIn("def _format_referral_screen", handlers_source)
        self.assertIn("<blockquote>", handlers_source)
        self.assertIn("reply_markup=inline_back_to_menu_keyboard()", handlers_source)
        self.assertIn('parse_mode="HTML"', handlers_source)
        self.assertNotIn("Реферальная программа пока ещё не готова", handlers_source)
        self.assertNotIn("USDT", handlers_source.upper())
        self.assertNotIn("🪁", handlers_source)

    def test_bot_screens_edit_inline_messages_and_replace_bottom_keyboard(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        regular_screen_source = handlers_source.split(
            "async def _show_onboarding_followup", 1
        )[0]
        show_screen_source = handlers_source.split(
            "async def _show_screen", 1
        )[1].split("async def _show_onboarding_followup", 1)[0]
        send_screen_source = handlers_source.split(
            "async def _send_screen_message", 1
        )[1].split("async def _show_screen", 1)[0]

        self.assertIn("edit_message_text", handlers_source)
        self.assertIn("edit_message_reply_markup", handlers_source)
        self.assertIn("message is not modified", handlers_source)
        self.assertIn("last_reply_keyboard_signature", handlers_source)
        self.assertIn("def _is_user_message", handlers_source)
        self.assertIn("Bottom-keyboard actions arrive as user messages", handlers_source)
        self.assertIn("Inline callback actions keep editing the message", handlers_source)
        self.assertIn("delete_current and _is_user_message(message)", show_screen_source)
        self.assertIn("async def _delete_screen_messages", regular_screen_source)
        self.assertIn("reply_markup=reply_markup", send_screen_source)
        self.assertNotIn("reply_markup=ReplyKeyboardRemove()", send_screen_source)
        self.assertNotIn("isinstance(reply_markup, InlineKeyboardMarkup)", send_screen_source)
        self.assertNotIn("edit_message_reply_markup", send_screen_source)
        self.assertNotIn("async def _refresh_reply_keyboard", handlers_source)
        self.assertNotIn("KEYBOARD_REFRESH_TEXT", handlers_source)
        self.assertNotIn("async def _remove_reply_keyboard", handlers_source)
        self.assertIn("await _delete_screen_messages(message, tracked_ids)", show_screen_source)
        self.assertLess(
            show_screen_source.index("await _delete_screen_messages(message, tracked_ids)"),
            show_screen_source.index("sent = await _send_screen_message"),
        )
        self.assertIn("await message.bot.delete_message", handlers_source)

    def test_user_messages_are_deleted_after_processing(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("async def _delete_user_message", handlers_source)
        self.assertIn("if not _is_user_message(message):", handlers_source)
        self.assertIn("message_id=message.message_id", handlers_source)
        self.assertIn("except (TelegramBadRequest, TelegramForbiddenError):", handlers_source)
        for handler in (
            "admin_command",
            "start",
            "help_command",
            "menu_command",
            "profile_command",
            "prompt_or_fallback",
        ):
            handler_source = handlers_source.split(
                f"async def {handler}(message: Message) -> None:", 1
            )[1].split("@router.", 1)[0]
            self.assertIn("await _delete_user_message(message)", handler_source)
            self.assertLess(
                handler_source.index("await _delete_user_message(message)"),
                handler_source.index("services.users.ensure_telegram_user"),
            )

    def test_start_onboarding_copy_and_continue_callback_are_present(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("Приветствую в Cea AI", handlers_source)
        self.assertIn("Документ оферты здесь", handlers_source)
        self.assertIn("(Документ оферты здесь: {offer_url}).", handlers_source)
        self.assertIn("DEFAULT_PUBLIC_OFFER_URL", handlers_source)
        self.assertIn("Чтобы узнать больше о своём аккаунте и тарифах", handlers_source)
        self.assertIn("«Профиль».", handlers_source)
        self.assertNotIn("«Меню» снизу слева от поля ввода текста", handlers_source)
        self.assertIn("В двух словах об основных инструментах", handlers_source)
        self.assertNotIn("☝️ В двух словах об основных инструментах", handlers_source)
        self.assertIn("ONBOARDING_PROMO_IMAGE_PATH", handlers_source)
        self.assertIn("assets", handlers_source)
        self.assertIn("onboarding_promo.jpeg", handlers_source)
        self.assertIn("FSInputFile(ONBOARDING_PROMO_IMAGE_PATH)", handlers_source)
        self.assertIn("send_photo", handlers_source)
        self.assertIn("caption=_format_onboarding_promo()", handlers_source)
        self.assertIn("самым современным и мощным", handlers_source)
        self.assertIn("генерация фото, генерация видео", handlers_source)
        self.assertIn("Если возникнут вопросы", handlers_source)
        self.assertIn("обращайтесь в поддержку", handlers_source)
        self.assertTrue(Path("ceai/assets/onboarding_promo.jpeg").exists())
        self.assertIn("_format_main_menu()", handlers_source)
        self.assertIn("menu.message_id", handlers_source)
        onboarding_followup_source = handlers_source.split(
            "async def _show_onboarding_followup", 1
        )[1].split("def _profile_link", 1)[0]
        self.assertIn("LAST_BOT_MESSAGE_IDS: [menu.message_id]", onboarding_followup_source)
        self.assertNotIn("hint.message_id", onboarding_followup_source)
        self.assertNotIn("promo.message_id", onboarding_followup_source)
        self.assertIn('F.data == "onboarding:continue"', handlers_source)
        self.assertIn("last_bot_message_ids", handlers_source)
        self.assertNotIn(
            "Документ оферты будет доступен после настройки ссылки", handlers_source
        )
        self.assertNotIn(
            "Добро пожаловать в CeaAI MVP. Здесь все AI и платежи",
            handlers_source,
        )

    def test_start_text_resets_any_dialog_state_before_prompt_handling(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        fallback_source = handlers_source.split(
            "async def prompt_or_fallback", 1
        )[1]

        self.assertIn('START_TEXT_ALIASES = {"старт", "/старт", "start", "/start", "начать"}', handlers_source)
        self.assertIn("_send_referral_join_notice", handlers_source)
        self.assertIn(
            "По вашей партнёрской ссылке пришел новый пользователь 🔥",
            handlers_source,
        )
        self.assertIn("ℹ️ ID:", handlers_source)
        self.assertIn("already_registered", handlers_source)
        self.assertIn("_send_referral_already_registered_notice", handlers_source)
        self.assertIn("❌ Вы уже зарегистрированы в Cea AI.", handlers_source)
        self.assertIn(
            "Партнёрская ссылка действует только для новых пользователей.",
            handlers_source,
        )
        already_registered_branch = handlers_source.split(
            "if referral_result.already_registered:", 1
        )[1].split("await _send_referral_join_notice", 1)[0]
        self.assertIn(
            "await _send_referral_already_registered_notice(message)",
            already_registered_branch,
        )
        self.assertNotIn("_show_screen", already_registered_branch)
        self.assertIn("if _is_start_text(message.text):", fallback_source)
        self.assertLess(
            fallback_source.index("if _is_start_text(message.text):"),
            fallback_source.index('session["state"] in {"admin_waiting_search", "admin_waiting_credit"}'),
        )
        self.assertLess(
            fallback_source.index("if _is_start_text(message.text):"),
            fallback_source.index('session["state"] == "waiting_text_chat_prompt"'),
        )

    def test_onboarding_keyboards_have_expected_buttons_only(self) -> None:
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")

        self.assertIn("Продолжить", keyboard_source)
        self.assertIn("onboarding:continue", keyboard_source)
        self.assertIn("Cea Family", keyboard_source)
        self.assertIn("Поддержка", keyboard_source)
        self.assertIn("https://t.me/{username}", keyboard_source)
        self.assertNotIn("Все ай ай инфо", keyboard_source)
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

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test"}, clear=True),
        ):
            settings = load_settings()

        self.assertEqual(settings.public_offer_url, DEFAULT_PUBLIC_OFFER_URL)
        self.assertEqual(settings.info_channel_url, DEFAULT_INFO_CHANNEL_URL)

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "RAILWAY_PUBLIC_DOMAIN": "cea-ai-production.up.railway.app",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.app_base_url, "https://cea-ai-production.up.railway.app")
        self.assertEqual(
            settings.public_offer_url,
            "https://cea-ai-production.up.railway.app/public-offer",
        )

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "APP_BASE_URL": "https://custom.example/",
                    "RAILWAY_PUBLIC_DOMAIN": "cea-ai-production.up.railway.app",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.app_base_url, "https://custom.example")

    def test_railway_deploy_config_uses_dockerfile_and_healthcheck(self) -> None:
        railway_config = loads(Path("railway.json").read_text(encoding="utf-8"))

        self.assertEqual(railway_config["build"]["builder"], "DOCKERFILE")
        self.assertEqual(railway_config["build"]["dockerfilePath"], "Dockerfile")
        self.assertEqual(railway_config["deploy"]["healthcheckPath"], "/healthz")

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

    def test_text_provider_instructions_identify_chatgpt_and_deepseek(self) -> None:
        from ceai.providers.identity import text_model_instructions

        chatgpt = text_model_instructions(
            {
                "provider": "openai",
                "model_key": "gpt-4o-mini",
                "display_name": "ChatGPT GPT-5.5",
            }
        )
        deepseek = text_model_instructions(
            {
                "provider": "deepseek",
                "model_key": "deepseek-v4-flash",
                "display_name": "DeepSeek V4 Flash",
            }
        )

        self.assertIn("ты ChatGPT", chatgpt)
        self.assertIn("ты DeepSeek", deepseek)
        self.assertIn("кто ты", chatgpt)
        self.assertIn("Telegram-боте Cea AI", deepseek)

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
        from ceai.formatting import (
            format_datetime_minute,
            format_datetime_russian_minute,
        )

        registered_at = format_datetime_minute("2026-06-20T11:22:33.123456+00:00")
        last_seen_at = format_datetime_minute("2026-06-20T11:25:59+00:00")
        subscription_ends_at = format_datetime_russian_minute(
            "2026-06-24T17:16:00+00:00"
        )

        self.assertEqual(registered_at, "20.06.2026 14:22")
        self.assertEqual(last_seen_at, "20.06.2026 14:25")
        self.assertEqual(subscription_ends_at, "24 июня 2026 года, 20:16")
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
            self.assertEqual(row["count"], 6)
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
