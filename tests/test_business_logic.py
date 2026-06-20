from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from ceai.config import Settings
from ceai.database import Database
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

    def test_telegram_commands_menu_contains_only_menu_and_profile(self) -> None:
        main_source = Path("ceai/main.py").read_text(encoding="utf-8")

        self.assertIn('BotCommand(command="menu", description="Главное меню")', main_source)
        self.assertIn('BotCommand(command="profile", description="Профиль")', main_source)
        self.assertNotIn('BotCommand(command="admin"', main_source)
        self.assertNotIn('BotCommand(command="help"', main_source)

    def test_menu_command_has_main_menu_copy(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("🏠 Главное меню", handlers_source)
        self.assertIn("Выберите нужный раздел 👇", handlers_source)
        self.assertIn("Command(\"menu\")", handlers_source)

    def test_migrations_record_applied_versions_once(self) -> None:
        db = Database("sqlite:///:memory:")
        try:
            db.migrate()
            db.migrate()
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM schema_migrations"
                ).fetchone()
            self.assertEqual(row["count"], 3)
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
