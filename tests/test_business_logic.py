from __future__ import annotations

import hashlib
import hmac
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
from ceai.providers.base import ImageInput, ProviderError
from ceai.providers.openai_image import OpenAIImageProvider
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

    def test_yookassa_payment_creation_uses_redirect_checkout(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            payment_provider="yookassa",
            app_base_url="https://bot.example",
            yookassa_shop_id="shop-test",
            yookassa_secret_key="secret-test",
        )
        services = build_services(self.db, settings)

        with patch.object(
            services.payments,
            "_yookassa_request",
            return_value={
                "id": "yk_payment_1",
                "status": "pending",
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": "https://yookassa.test/pay/1",
                },
            },
        ) as api:
            payment = services.payments.create_payment(
                user_id=self.user["id"],
                plan_code="start",
                payment_method="yookassa",
            )

        self.assertEqual(payment["provider"], "yookassa")
        self.assertEqual(payment["external_id"], "yk_payment_1")
        self.assertEqual(payment["payment_url"], "https://yookassa.test/pay/1")
        self.assertEqual(payment["amount_rub"], 299)

        args, kwargs = api.call_args
        self.assertEqual(args, ("POST", "/payments"))
        payload = kwargs["payload"]
        self.assertEqual(payload["amount"], {"value": "299.00", "currency": "RUB"})
        self.assertTrue(payload["capture"])
        self.assertEqual(payload["confirmation"]["type"], "redirect")
        self.assertEqual(
            payload["confirmation"]["return_url"],
            "https://bot.example/payments/yookassa/return",
        )
        self.assertEqual(payload["metadata"]["plan_code"], "start")

    def test_crypto_pay_payment_creation_uses_invoice_url(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            app_base_url="https://bot.example",
            crypto_pay_token="crypto-token",
            crypto_pay_api_base_url="https://testnet-pay.crypt.bot/api",
            crypto_pay_accepted_assets="USDT",
        )
        services = build_services(self.db, settings)

        with patch.object(
            services.payments,
            "_crypto_pay_request",
            return_value={
                "ok": True,
                "result": {
                    "invoice_id": 98765,
                    "status": "active",
                    "pay_url": "https://t.me/CryptoTestnetBot?start=invoice-98765",
                },
            },
        ) as api:
            payment = services.payments.create_payment(
                user_id=self.user["id"],
                plan_code="start",
                payment_method="usdt_trc20",
            )

        self.assertEqual(payment["provider"], "crypto_pay")
        self.assertEqual(payment["external_id"], "98765")
        self.assertEqual(
            payment["payment_url"],
            "https://t.me/CryptoTestnetBot?start=invoice-98765",
        )
        self.assertEqual(payment["amount_rub"], 299)

        args, kwargs = api.call_args
        self.assertEqual(args, ("createInvoice",))
        payload = kwargs["payload"]
        self.assertEqual(payload["currency_type"], "fiat")
        self.assertEqual(payload["fiat"], "RUB")
        self.assertEqual(payload["amount"], "299")
        self.assertEqual(payload["accepted_assets"], "USDT")
        self.assertIn("ceaai-crypto-", payload["payload"])

    def test_successful_crypto_pay_webhook_credits_coins_once(self) -> None:
        token = "crypto-token"
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            crypto_pay_token=token,
        )
        services = build_services(self.db, settings)

        with patch.object(
            services.payments,
            "_crypto_pay_request",
            return_value={
                "ok": True,
                "result": {
                    "invoice_id": 54321,
                    "status": "active",
                    "pay_url": "https://t.me/CryptoTestnetBot?start=invoice-54321",
                },
            },
        ):
            payment = services.payments.create_payment(
                user_id=self.user["id"],
                plan_code="start",
                payment_method="usdt_trc20",
            )

        payload = {
            "update_id": 100,
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": 54321,
                "status": "paid",
                "asset": "USDT",
                "amount": "299",
            },
        }
        raw_body = dumps(payload).encode("utf-8")
        signature = hmac.new(
            hashlib.sha256(token.encode("utf-8")).digest(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        first = services.payments.process_crypto_pay_webhook(
            payload=payload, raw_body=raw_body, signature=signature
        )
        second = services.payments.process_crypto_pay_webhook(
            payload=payload, raw_body=raw_body, signature=signature
        )

        self.assertTrue(first.processed)
        self.assertEqual(first.credited_coins, 100)
        self.assertFalse(second.processed)
        self.assertTrue(second.duplicate)
        self.assertEqual(services.subscriptions.balance_for_user(self.user["id"]), 100)

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

        with self.assertRaises(BusinessRuleError):
            services.payments.process_crypto_pay_webhook(
                payload=payload, raw_body=raw_body, signature="bad-signature"
            )

    def test_telegram_stars_payment_credits_coins_once(self) -> None:
        payment = self.services.payments.create_payment(
            user_id=self.user["id"],
            plan_code="start",
            payment_method="telegram_stars",
        )
        meta = loads_dict(payment["meta"])

        self.assertEqual(payment["provider"], "telegram_stars")
        self.assertTrue(payment["external_id"].startswith("stars_"))
        self.assertEqual(payment["payment_url"], f"telegram-stars://{payment['external_id']}")
        self.assertEqual(meta["stars_amount"], 150)
        self.assertEqual(meta["stars_fixed_amount"], 0)
        self.assertEqual(meta["coins_amount"], 100)
        self.assertEqual(meta["duration_days"], 30)

        checkout_payment = self.services.payments.validate_telegram_stars_pre_checkout(
            invoice_payload=payment["external_id"],
            currency="XTR",
            total_amount=150,
        )
        self.assertEqual(checkout_payment["id"], payment["id"])

        first = self.services.payments.process_telegram_stars_successful_payment(
            invoice_payload=payment["external_id"],
            currency="XTR",
            total_amount=150,
            telegram_payment_charge_id="tg-stars-charge-1",
            provider_payment_charge_id="",
        )
        second = self.services.payments.process_telegram_stars_successful_payment(
            invoice_payload=payment["external_id"],
            currency="XTR",
            total_amount=150,
            telegram_payment_charge_id="tg-stars-charge-1",
            provider_payment_charge_id="",
        )

        self.assertTrue(first.processed)
        self.assertEqual(first.credited_coins, 100)
        self.assertFalse(second.processed)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.services.subscriptions.balance_for_user(self.user["id"]), 100)

        with self.assertRaises(BusinessRuleError):
            self.services.payments.validate_telegram_stars_pre_checkout(
                invoice_payload=payment["external_id"],
                currency="XTR",
                total_amount=150,
            )

    def test_successful_yookassa_webhook_credits_coins_once(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            payment_provider="yookassa",
            app_base_url="https://bot.example",
            yookassa_shop_id="shop-test",
            yookassa_secret_key="secret-test",
        )
        services = build_services(self.db, settings)

        with patch.object(
            services.payments,
            "_yookassa_request",
            return_value={
                "id": "yk_payment_2",
                "status": "pending",
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": "https://yookassa.test/pay/2",
                },
            },
        ):
            payment = services.payments.create_payment(
                user_id=self.user["id"],
                plan_code="start",
                payment_method="yookassa",
            )

        payload = {
            "type": "notification",
            "event": "payment.succeeded",
            "object": {
                "id": "yk_payment_2",
                "status": "succeeded",
                "paid": True,
            },
        }
        with patch.object(
            services.payments,
            "_fetch_yookassa_payment",
            return_value={"id": "yk_payment_2", "status": "succeeded", "paid": True},
        ) as fetch:
            first = services.payments.process_yookassa_webhook(payload=payload)
            second = services.payments.process_yookassa_webhook(payload=payload)

        self.assertTrue(first.processed)
        self.assertEqual(first.credited_coins, 100)
        self.assertFalse(second.processed)
        self.assertTrue(second.duplicate)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(services.subscriptions.balance_for_user(self.user["id"]), 100)

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

    def test_image_generation_uses_standard_and_four_k_coin_costs(self) -> None:
        mock_services = build_services(
            self.db,
            Settings(
                telegram_bot_token="test",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
                ai_provider_mode="mock",
            ),
        )
        payment = mock_services.payments.create_mock_payment(
            user_id=self.user["id"], plan_code="start"
        )
        mock_services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        model = next(
            model
            for model in mock_services.catalog.list_models()
            if model["model_key"] == "gpt-image-2-medium"
        )

        standard = mock_services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Нарисуй футуристичный город",
        )
        four_k = mock_services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Нарисуй футуристичный город 4К",
        )

        self.assertEqual(standard.generation["coins_charged"], 2)
        self.assertEqual(standard.balance_after, 98)
        self.assertEqual(four_k.generation["coins_charged"], 3)
        self.assertEqual(four_k.balance_after, 95)
        history = mock_services.generations.list_recent(user_id=self.user["id"])
        self.assertEqual(history[0]["prompt_payload"]["image_resolution"], "4k")

    def test_image_generation_accepts_uploaded_image_input(self) -> None:
        mock_services = build_services(
            self.db,
            Settings(
                telegram_bot_token="test",
                database_url="sqlite:///:memory:",
                app_env="test",
                mock_payment_base_url="https://mock-payments.test/pay",
                ai_provider_mode="mock",
            ),
        )
        payment = mock_services.payments.create_mock_payment(
            user_id=self.user["id"], plan_code="start"
        )
        mock_services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment["id"]
        )
        model = next(
            model
            for model in mock_services.catalog.list_models()
            if model["model_key"] == "gpt-image-2-medium"
        )
        image_input = ImageInput(
            data=b"fake image bytes",
            mime_type="image/png",
            file_name="source.png",
        )

        result = mock_services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Сделай фон светлее",
            image_input=image_input,
        )

        self.assertEqual(result.generation["status"], "completed")
        self.assertEqual(result.generation["coins_charged"], 2)
        self.assertEqual(result.balance_after, 98)
        self.assertIn("изменение изображения", result.result["caption"])
        history = mock_services.generations.list_recent(user_id=self.user["id"])
        self.assertEqual(
            history[0]["prompt_payload"]["image_input"],
            {
                "file_name": "source.png",
                "mime_type": "image/png",
                "size_bytes": len(image_input.data),
            },
        )

    def test_openai_image_provider_uses_edit_endpoint_for_image_input(self) -> None:
        provider = OpenAIImageProvider(api_key="test-key")
        calls = []

        def fake_post_json(path, payload):
            calls.append((path, payload))
            return {"created": 123, "data": [{"b64_json": "aW1hZ2U="}]}

        provider._post_json = fake_post_json

        result = provider.generate(
            model={
                "provider": "openai",
                "model_key": "gpt-image-2-medium",
                "display_name": "GPT Image 2",
                "generation_type": "image",
                "config": {
                    "api_model": "gpt-image-2",
                    "quality": "medium",
                    "size": "1024x1024",
                    "output_format": "png",
                },
            },
            prompt_text="Поменяй фон на белый",
            image_input=ImageInput(
                data=b"image",
                mime_type="image/png",
                file_name="photo.png",
            ),
        )

        self.assertEqual(calls[0][0], "/images/edits")
        self.assertEqual(calls[0][1]["model"], "gpt-image-2")
        self.assertTrue(
            calls[0][1]["images"][0]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertEqual(result.result["kind"], "image")
        self.assertIn("Изменение изображения", result.result["caption"])

    def test_generation_recovers_active_subscription_from_paid_payment(self) -> None:
        payment = self.services.payments.create_mock_payment(
            user_id=self.user["id"], plan_code="start"
        )
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE payments
                SET status = 'paid', paid_at = created_at, subscription_id = NULL
                WHERE id = ?
                """,
                (payment["id"],),
            )

        model = self._model("deepseek-v4-flash")
        result = self.services.generations.generate(
            user_id=self.user["id"],
            model_price_id=model["id"],
            prompt_text="Проверь подписку",
        )

        self.assertEqual(result.generation["status"], "completed")
        self.assertEqual(result.balance_after, 99)
        subscription = self.services.subscriptions.active_for_user(self.user["id"])
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription["coins_balance_cache"], 99)
        with self.db.transaction() as conn:
            recovered_payment = conn.execute(
                "SELECT subscription_id FROM payments WHERE id = ?", (payment["id"],)
            ).fetchone()
            credit_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM coin_transactions
                WHERE payment_id = ? AND type = 'credit'
                """,
                (payment["id"],),
            ).fetchone()
        self.assertIsNotNone(recovered_payment["subscription_id"])
        self.assertEqual(credit_count["count"], 1)

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
        from ceai.bot.handlers import (
            _format_menu,
            _format_referral_screen,
            _format_referral_withdrawal_unavailable,
        )

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
        self.assertIn("💰 Баланс: 0 коинов", profile)
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
        self.assertIn("💰 Баланс: 42 коина", active_profile)
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

        unavailable = _format_referral_withdrawal_unavailable(100_000)
        self.assertIn("❌ <b>Вывод средств сейчас недоступен.</b>", unavailable)
        self.assertIn(
            "Вывод доступен при реферальном балансе от 1000 рублей.",
            unavailable,
        )

    def test_failed_generation_refunds_reserved_coins(self) -> None:
        self._buy_plan("start")
        model = self._model("deepseek-v4-flash")

        with self.assertRaises(GenerationProviderFailedError) as raised:
            self.services.generations.generate(
                user_id=self.user["id"],
                model_price_id=model["id"],
                prompt_text="mock_error",
            )

        self.assertIn("Коины возвращены", str(raised.exception))
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

    def test_image_provider_errors_are_user_readable(self) -> None:
        from ceai.services.generations import _provider_error_message

        self.assertIn(
            "не задан ключ OpenAI Image",
            _provider_error_message(
                provider_error=(
                    "OpenAI Image provider is not configured. "
                    "Set OPENAI_IMAGE_API_KEY or OPENAI_API_KEY."
                ),
                generation_type="image",
            ),
        )
        self.assertIn(
            "OpenAI не принял API-ключ",
            _provider_error_message(
                provider_error="OpenAI Image API returned HTTP 401: invalid key",
                generation_type="image",
            ),
        )
        self.assertIn(
            "Organization Verification",
            _provider_error_message(
                provider_error=(
                    "OpenAI Image API returned HTTP 403: "
                    "organization verification required"
                ),
                generation_type="image",
            ),
        )

    def test_handlers_show_provider_failure_text(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("except GenerationProviderFailedError as exc", handlers_source)
        self.assertIn("str(exc)", handlers_source)
        self.assertNotIn(
            '"Не получилось выполнить генерацию. Коины возвращены.",',
            handlers_source,
        )

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

        self.assertIn('TEXT_AI_BUTTON = "💡Нейросети: ChatGPT, DeepSeek"', keyboard_source)
        self.assertNotIn("🤖 Нейронки: ChatGPT, DeepSeek", keyboard_source)
        self.assertNotIn("DeepSeq", keyboard_source)
        self.assertNotIn("DeepSeak", keyboard_source)

    def test_ai_reply_keyboards_have_back_button_only(self) -> None:
        from ceai.bot.handlers import _format_direct_prompt_screen
        from ceai.bot.handlers import _format_image_generation_caption
        from ceai.bot.keyboards import model_choice_label

        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        seed_source = Path("ceai/seed.py").read_text(encoding="utf-8")
        model_keyboard_source = keyboard_source.split(
            "def models_keyboard(", 1
        )[1].split("def model_choice_label", 1)[0]

        self.assertIn('BACK_TO_MENU_BUTTON = "⬅️ Назад"', keyboard_source)
        self.assertIn(
            "def models_keyboard(models: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup",
            keyboard_source,
        )
        self.assertIn("InlineKeyboardButton(", model_keyboard_source)
        self.assertIn("text=model_choice_label(model)", model_keyboard_source)
        self.assertIn('callback_data=f"model:{model[\'id\']}"', model_keyboard_source)
        self.assertNotIn("ReplyKeyboardMarkup", model_keyboard_source)
        self.assertIn("state=\"waiting_model_choice\"", handlers_source)
        self.assertIn("reply_markup=models_keyboard(models)", handlers_source)
        self.assertIn("screen_text = (", handlers_source)
        self.assertIn('if generation_types == {"text"}', handlers_source)
        self.assertIn('F.data.startswith("models:type:")', handlers_source)
        self.assertIn("💡Выберите текстовую модель:", handlers_source)
        self.assertIn("ui_description", handlers_source)
        self.assertIn("ui_description", seed_source)
        self.assertIn(
            "Стоимость: {format_coin_amount(model['coins_cost'])} за запрос.",
            handlers_source,
        )
        self.assertNotIn('lines = ["Выберите AI-инструмент:"]', handlers_source)
        self.assertIn("reply_markup=back_to_menu_keyboard()", handlers_source)
        self.assertIn("def back_to_menu_keyboard() -> InlineKeyboardMarkup", keyboard_source)
        self.assertIn('InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")', keyboard_source)
        self.assertIn("Выберите модель кнопкой в сообщении.", handlers_source)
        self.assertIn("skip_single_model_choice=True", handlers_source)
        self.assertIn("Модель: {model['display_name']}", handlers_source)
        self.assertIn("Стоимость 1 запроса 4К", handlers_source)
        self.assertIn("🔎Чтобы получить изображение 4К", handlers_source)
        self.assertIn('"Запускаю генерацию..."', handlers_source)
        launch_blocks = handlers_source.split('"Запускаю генерацию..."')[1:]
        self.assertGreaterEqual(len(launch_blocks), 2)
        for block in launch_blocks:
            self.assertIn("reply_markup=None", block.split(")", 1)[0])
        self.assertNotIn(
            '"Запускаю генерацию...",\n'
            "                reply_markup=back_to_menu_keyboard()",
            handlers_source,
        )
        self.assertNotIn(
            '"Запускаю генерацию...",\n'
            "                reply_markup=chat_keyboard",
            handlers_source,
        )
        self.assertIn("_image_input_from_message", handlers_source)
        self.assertIn("DEFAULT_IMAGE_EDIT_PROMPT", handlers_source)
        self.assertIn("_format_image_generation_caption", handlers_source)
        self.assertIn("payload.pop(LAST_BOT_MESSAGE_IDS, None)", handlers_source)
        self.assertIn("_show_generation_result(", handlers_source)
        self.assertIn("BufferedInputFile", handlers_source)
        self.assertNotIn("Баланс после генерации", handlers_source)
        self.assertNotIn('"Запускаю mock-генерацию..."', handlers_source)
        self.assertEqual(
            model_choice_label(
                {"generation_type": "text", "display_name": "DeepSeek V4 Flash"}
            ),
            "DeepSeek V4 Flash",
        )
        self.assertEqual(
            model_choice_label(
                {"generation_type": "text", "display_name": "ChatGPT GPT-5.5"}
            ),
            "ChatGPT GPT-5.5",
        )
        self.assertEqual(
            _format_direct_prompt_screen(
                {
                    "display_name": "GPT Image 2",
                    "generation_type": "image",
                    "coins_cost": 2,
                    "config": {"four_k_coins_cost": 3},
                }
            ),
            "Модель: GPT Image 2\n\n"
            "Стоимость 1 запроса: 2 Coin\n"
            "Стоимость 1 запроса 4К: 3 Coin\n\n"
            "Введите текст для генерации или изображение которое хотите изменить.\n\n"
            "🔎Чтобы получить изображение 4К, добавьте «4К» в текст запроса",
        )
        self.assertEqual(
            _format_image_generation_caption(
                prompt_text="Создай фото милого котика",
                model={"display_name": "GPT Image 2"},
                coins_charged=3,
                balance_after=146,
            ),
            "📍 Ваш запрос: Создай фото милого котика\n\n"
            "🎛️ Инструмент: GPT Image 2\n\n"
            "ℹ️ Списано: 3 Coin  Баланс: 146.000 Coin",
        )

    def test_video_and_tts_sections_show_unavailable_stub(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("_feature_temporarily_unavailable_message", handlers_source)
        self.assertIn("❌ Функция временно недоступна.", handlers_source)
        self.assertIn("находится в технической подготовке", handlers_source)
        self.assertIn('generation_type in {"video", "tts"}', handlers_source)
        self.assertIn('_feature_temporarily_unavailable_message("Видео с AI")', handlers_source)
        self.assertIn('_feature_temporarily_unavailable_message("Озвучка с AI")', handlers_source)
        self.assertIn("reply_markup=back_to_menu_keyboard()", handlers_source)

    def test_plan_screen_uses_new_prices_and_coins_are_called_koiny(self) -> None:
        from ceai.bot.handlers import (
            _format_crystal_packages,
            _format_plan_details,
            _format_plans,
        )
        from ceai.bot.keyboards import (
            crystal_packages_keyboard,
            main_menu_button_keyboard,
            payment_methods_keyboard,
            plans_keyboard,
        )
        from ceai.seed import PLANS

        text = _format_plans(PLANS)
        crystal_text = _format_crystal_packages()
        start_plan = next(plan for plan in PLANS if plan["code"] == "start")
        basic_plan = next(plan for plan in PLANS if plan["code"] == "basic")
        pro_plan = next(plan for plan in PLANS if plan["code"] == "pro")
        start_details = _format_plan_details(start_plan)
        basic_details = _format_plan_details(basic_plan)
        pro_details = _format_plan_details(pro_plan)
        labels = [row[0].text for row in plans_keyboard(PLANS).inline_keyboard]
        callbacks = [
            row[0].callback_data for row in plans_keyboard(PLANS).inline_keyboard
        ]
        crystal_labels = [
            row[0].text for row in crystal_packages_keyboard().inline_keyboard
        ]
        crystal_callbacks = [
            row[0].callback_data
            for row in crystal_packages_keyboard().inline_keyboard
        ]
        payment_method_labels = [
            row[0].text for row in payment_methods_keyboard("start").inline_keyboard
        ]
        payment_method_callbacks = [
            row[0].callback_data
            for row in payment_methods_keyboard("start").inline_keyboard
        ]
        main_menu_button = main_menu_button_keyboard().inline_keyboard[0][0]
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertEqual(
            text,
            "💳 Выберите тариф с подпиской.\n\n"
            "Нажмите на любой тариф ниже — покажу цену, количество коинов и что входит.",
        )
        self.assertNotIn("Старт", text)
        self.assertNotIn("Базовый", text)
        self.assertNotIn("Про", text)
        self.assertNotIn("Купить коины отдельно", text)
        self.assertNotIn("coins", text.casefold())
        self.assertEqual(
            crystal_text,
            "💳 Выберите количество коинов для покупки:",
        )
        self.assertIn("⭐️ Старт - 299руб", labels)
        self.assertIn("🔥 Базовый - 699руб", labels)
        self.assertIn("⚡️ Про - 1490руб", labels)
        self.assertIn("Купить коины отдельно", labels)
        self.assertEqual(
            crystal_labels,
            [
                "S - 139₽ - 30 коинов",
                "🔥M - 499₽ - 110 коинов  (-2%)",
                "L - 999₽ - 260 коинов  (-17%)",
                "XL - 2990₽ - 1198 коинов  (-45%)",
                "⚡XXL - 9000₽ - 4300 коинов  (-55%)",
                "⬅️ Назад",
            ],
        )
        self.assertIn("crystals:s", crystal_callbacks)
        self.assertIn("crystals:xxl", crystal_callbacks)
        self.assertIn("⭐️ Старт — 299 ₽", start_details)
        self.assertIn("➕ 100 коинов", start_details)
        self.assertIn("➕ До 100 запросов DeepSeek", start_details)
        self.assertIn("➕ До 33 запросов ChatGPT", start_details)
        self.assertIn("➕ До 50 изображений GPT Image", start_details)
        self.assertIn("⭐ Telegram Stars: 150⭐", start_details)
        self.assertIn("💳 Выберите способ оплаты:", start_details)
        self.assertIn("🔥 Базовый — 699 ₽", basic_details)
        self.assertIn("➕ 230 коинов", basic_details)
        self.assertIn("➕ До 76 запросов ChatGPT", basic_details)
        self.assertIn("➕ До 115 изображений GPT Image", basic_details)
        self.assertIn("⭐ Telegram Stars: 350⭐", basic_details)
        self.assertIn("⚡️ Про — 1490 ₽", pro_details)
        self.assertIn("➕ 500 коинов", pro_details)
        self.assertIn("➕ До 166 запросов ChatGPT", pro_details)
        self.assertIn("➕ До 250 изображений GPT Image", pro_details)
        self.assertIn("⭐ Telegram Stars: 745⭐", pro_details)
        self.assertEqual(
            {plan["code"]: plan["coins_amount"] for plan in PLANS},
            {"start": 100, "basic": 230, "pro": 500},
        )
        self.assertIn("coins:buy", callbacks)
        self.assertEqual(
            payment_method_labels[:3],
            ["💳 Карта / СБП", "⭐️ Telegram Stars", "⬅️ Назад"],
        )
        self.assertIn("pay_method:start:card_sbp", payment_method_callbacks)
        self.assertNotIn("pay_method:start:usdt_trc20", payment_method_callbacks)
        self.assertNotIn("Крипта", payment_method_labels)
        self.assertIn("pay_method:start:telegram_stars", payment_method_callbacks)
        self.assertEqual(main_menu_button.text, "🏠 Главное меню")
        self.assertEqual(main_menu_button.callback_data, "menu:main")
        self.assertIn("💳 Выберите способ оплаты:", handlers_source)
        self.assertIn("_format_plan_details(plan)", handlers_source)
        self.assertIn('state="waiting_payment_method"', handlers_source)
        self.assertIn('F.data.startswith("pay_method:")', handlers_source)
        self.assertNotIn('payment_method == "card_sbp"', handlers_source)
        self.assertNotIn('"Этот способ оплаты скоро будет подключён."', handlers_source)
        self.assertNotIn("reply_markup=payment_unavailable_keyboard()", handlers_source)
        self.assertIn("_send_telegram_stars_invoice", handlers_source)
        self.assertIn('currency="XTR"', handlers_source)
        self.assertIn("Подписка CeaAI", handlers_source)
        self.assertIn("Коины начислятся автоматически после оплаты.", handlers_source)
        self.assertIn("TELEGRAM_STARS_INVOICE_MESSAGE_ID", handlers_source)
        self.assertIn("_delete_telegram_stars_invoice_message", handlers_source)
        self.assertIn("reply_markup=main_menu_button_keyboard()", handlers_source)
        self.assertIn("Начислено {format_coin_amount(result.credited_coins)}", handlers_source)
        self.assertIn("@router.pre_checkout_query()", handlers_source)
        self.assertIn("@router.message(F.successful_payment)", handlers_source)
        self.assertIn('F.data == "coins:buy"', handlers_source)
        self.assertIn('F.data.startswith("crystals:")', handlers_source)
        self.assertIn("_format_crystal_packages()", handlers_source)
        self.assertIn("Покупка коинов скоро будет доступна.", handlers_source)

    def test_text_chat_navigation_has_back_and_no_premature_current_chat(
        self,
    ) -> None:
        from ceai.bot.handlers import (
            _format_text_chat_list_screen,
            _format_text_chat_prompt_screen,
        )

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
        self.assertIn("InlineKeyboardButton(", chat_keyboard_source)
        self.assertIn("text=text_chat_label", chat_keyboard_source)
        self.assertIn('callback_data=f"text_chat:select:{chat[\'id\']}"', chat_keyboard_source)
        self.assertIn('callback_data="text_chat:add"', chat_keyboard_source)
        self.assertIn('callback_data="menu:main"', chat_keyboard_source)
        self.assertNotIn("ReplyKeyboardMarkup", chat_keyboard_source)
        self.assertNotIn('TEXT_CHAT_LIST_BUTTON = "К чатам"', keyboard_source)
        self.assertNotIn('"К чатам"', keyboard_source)
        self.assertNotIn('"Текущий чат:', handlers_source)
        self.assertNotIn("Введите текст, что хотите спросить у нейросетки.", handlers_source)
        self.assertNotIn('prefix = "✓ "', keyboard_source)
        self.assertIn("def text_chat_keyboard(", keyboard_source)
        self.assertIn("def text_chat_prompt_keyboard(", keyboard_source)
        self.assertIn("def text_chat_prompt_keyboard() -> InlineKeyboardMarkup", keyboard_source)
        self.assertIn("InlineKeyboardButton(", prompt_keyboard_source)
        self.assertIn("text=DELETE_CURRENT_TEXT_CHAT_BUTTON", prompt_keyboard_source)
        self.assertIn('callback_data="text_chat:delete"', prompt_keyboard_source)
        self.assertIn('callback_data="text_chat:back"', prompt_keyboard_source)
        self.assertNotIn("ReplyKeyboardMarkup", prompt_keyboard_source)
        self.assertIn("state=\"waiting_text_chat_choice\"", handlers_source)
        self.assertIn('if action == "back":', handlers_source)
        self.assertIn("current_text_chat_id\": int(current_chat[\"id\"]) if current_chat else 0", handlers_source)
        self.assertIn('F.data.startswith("text_chat:")', handlers_source)
        self.assertIn("Выберите чат кнопкой в сообщении.", handlers_source)
        self.assertIn("waiting_text_chat_prompt", handlers_source)
        self.assertIn("waiting_text_chat_name", handlers_source)
        self.assertIn("text_chat_id", handlers_source)
        self.assertIn("text_chat_system_prompt", handlers_source)
        self.assertEqual(
            _format_text_chat_list_screen(
                {"display_name": "DeepSeek V4 Flash", "coins_cost": 1}
            ),
            "💡DeepSeek V4 Flash\n\n"
            "Стоимость 1 запроса: 1 Coin\n"
            "Выберите чат ниже:",
        )
        self.assertEqual(
            _format_text_chat_list_screen(
                {"display_name": "ChatGPT GPT-5.5", "coins_cost": 3}
            ),
            "💡ChatGPT GPT-5.5\n\n"
            "Стоимость 1 запроса: 3 Coin\n"
            "Выберите чат ниже:",
        )
        self.assertEqual(
            _format_text_chat_prompt_screen(
                {"display_name": "DeepSeek V4 Flash", "coins_cost": 1},
                {"title": "Основной"},
            ),
            "💡DeepSeek V4 Flash\n\n"
            "Стоимость 1 запроса: 1 Coin\n"
            "Чат «Основной» выбран.\n\n"
            "Введите текст, что хотите спросить у нейросети.",
        )
        self.assertEqual(
            _format_text_chat_prompt_screen(
                {"display_name": "ChatGPT GPT-5.5", "coins_cost": 3},
                {"title": "Основной"},
            ),
            "💡ChatGPT GPT-5.5\n\n"
            "Стоимость 1 запроса: 3 Coin\n"
            "Чат «Основной» выбран.\n\n"
            "Введите текст, что хотите спросить у нейросети.",
        )

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

    def test_admin_inline_buttons_edit_existing_screen(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        admin_callback_source = handlers_source.split(
            '@router.callback_query(F.data.startswith("admin:"))', 1
        )[1].split('@router.callback_query(F.data == "menu:home")', 1)[0]
        admin_message_source = handlers_source.split(
            'session["state"] in {"admin_waiting_search", "admin_waiting_credit"}', 1
        )[1].split("if _is_blocked_regular_user(services, user):", 1)[0]

        self.assertIn("_show_screen(", admin_callback_source)
        self.assertIn("_track_existing_screen_message", handlers_source)
        self.assertIn(
            '_track_existing_screen_message(services, user["id"], callback.message)',
            admin_callback_source,
        )
        self.assertIn("_send_admin_home(callback.message, services, user[\"id\"])", admin_callback_source)
        self.assertNotIn("delete_current=True", admin_callback_source)
        self.assertIn("_show_screen(", admin_message_source)
        self.assertNotIn("delete_current=True", admin_message_source)

    def test_menu_command_has_main_menu_copy(self) -> None:
        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")

        self.assertIn("🏠 Главное меню", handlers_source)
        self.assertIn("Выберите нужный раздел 👇", handlers_source)
        self.assertIn("Command(\"menu\")", handlers_source)

    def test_profile_screen_has_inline_actions_and_no_bottom_prompt(self) -> None:
        from ceai.bot.keyboards import profile_keyboard, referral_keyboard

        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        keyboard_source = Path("ceai/bot/keyboards.py").read_text(encoding="utf-8")
        profile_format_source = handlers_source.split(
            "def _format_menu(", 1
        )[1].split("def _format_onboarding_greeting", 1)[0]
        referral_source = handlers_source.split(
            "def _format_referral_screen", 1
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
        referral_row = referral_keyboard().inline_keyboard[0]
        self.assertEqual([button.text for button in referral_row], ["💰 Вывести", "⬅️ Назад"])
        self.assertEqual(
            [button.callback_data for button in referral_row],
            ["referral:withdraw", "menu:main"],
        )
        self.assertIn("Подписка и тарифы", keyboard_source)
        self.assertNotIn("🏠 Главное меню", labels)
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
        self.assertIn("reply_markup=referral_keyboard()", handlers_source)
        self.assertIn('F.data == "referral:withdraw"', handlers_source)
        self.assertIn("_format_referral_withdrawal_unavailable", handlers_source)
        self.assertIn("withdrawal_min_kopecks", handlers_source)
        self.assertIn('parse_mode="HTML"', handlers_source)
        self.assertNotIn("Реферальная программа пока ещё не готова", handlers_source)
        self.assertNotIn("USDT", referral_source.upper())
        self.assertNotIn("🪁", handlers_source)

    def test_subscription_required_message_has_plan_buttons_without_test_copy(self) -> None:
        from ceai.bot.handlers import _subscription_required_message
        from ceai.bot.keyboards import payment_keyboard, subscription_required_keyboard

        handlers_source = Path("ceai/bot/handlers.py").read_text(encoding="utf-8")
        keyboard = subscription_required_keyboard()
        labels = [row[0].text for row in keyboard.inline_keyboard]
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        payment_labels = [
            row[0].text
            for row in payment_keyboard(1, "https://pay.example").inline_keyboard
        ]

        self.assertEqual(labels, ["💳 Подписка и тарифы"])
        self.assertEqual(callbacks, ["menu:plans"])
        self.assertEqual(
            _subscription_required_message(),
            "Нужна активная подписка. Откройте тарифы и выберите подписку.",
        )
        self.assertIn("reply_markup=subscription_required_keyboard()", handlers_source)
        self.assertNotIn("оплатите тестово", handlers_source.casefold())
        self.assertNotIn("Оплатить тестово", payment_labels)
        self.assertNotIn("Тестовая ссылка оплаты", payment_labels)

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
        )[1].split("async def _remove_legacy_reply_keyboard", 1)[0]

        self.assertIn("edit_message_text", handlers_source)
        self.assertIn("edit_message_reply_markup", handlers_source)
        self.assertIn("message is not modified", handlers_source)
        self.assertIn("last_reply_keyboard_signature", handlers_source)
        self.assertIn("async def _remove_legacy_reply_keyboard", handlers_source)
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

    def test_crypto_pay_env_settings_are_read(self) -> None:
        from ceai.config import load_settings

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "CRYPTO_PAY_TOKEN": "crypto-token",
                    "CRYPTO_PAY_API_BASE": "https://testnet-pay.crypt.bot",
                    "CRYPTO_PAY_WEBHOOK_SECRET": "crypto-secret",
                    "CRYPTO_PAY_WEBHOOK_PATH": "/crypto/hook",
                    "CRYPTO_PAY_ACCEPTED_ASSETS": "USDT,TON",
                    "CRYPTO_PAY_REQUEST_TIMEOUT_SECONDS": "7",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.crypto_pay_token, "crypto-token")
        self.assertEqual(
            settings.crypto_pay_api_base_url, "https://testnet-pay.crypt.bot"
        )
        self.assertEqual(settings.crypto_pay_webhook_secret, "crypto-secret")
        self.assertEqual(settings.crypto_pay_webhook_path, "/crypto/hook")
        self.assertEqual(settings.crypto_pay_accepted_assets, "USDT,TON")
        self.assertEqual(settings.crypto_pay_request_timeout_seconds, 7)

    def test_telegram_stars_env_settings_are_read(self) -> None:
        from ceai.config import load_settings

        with (
            patch("ceai.config._load_dotenv", return_value={}),
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "TELEGRAM_STARS_AMOUNT": "7",
                },
                clear=True,
            ),
        ):
            settings = load_settings()

        self.assertEqual(settings.telegram_stars_amount, 7)

    def test_railway_deploy_config_uses_dockerfile_and_healthcheck(self) -> None:
        railway_config = loads(Path("railway.json").read_text(encoding="utf-8"))

        self.assertEqual(railway_config["build"]["builder"], "DOCKERFILE")
        self.assertEqual(railway_config["build"]["dockerfilePath"], "Dockerfile")
        self.assertEqual(railway_config["deploy"]["healthcheckPath"], "/healthz")

    def test_production_refuses_ephemeral_sqlite(self) -> None:
        from ceai.main import _ensure_persistent_database

        sqlite_settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///./data/ceai.sqlite3",
            app_env="production",
            mock_payment_base_url="https://mock-payments.test/pay",
        )
        with self.assertRaisesRegex(SystemExit, "Refusing to start with SQLite"):
            _ensure_persistent_database(sqlite_settings)

        _ensure_persistent_database(
            Settings(
                telegram_bot_token="test",
                database_url="postgresql://user:password@host:5432/dbname",
                app_env="production",
                mock_payment_base_url="https://mock-payments.test/pay",
            )
        )
        _ensure_persistent_database(
            Settings(
                telegram_bot_token="test",
                database_url="sqlite:///./data/ceai.sqlite3",
                app_env="production",
                mock_payment_base_url="https://mock-payments.test/pay",
                allow_ephemeral_sqlite=True,
            )
        )

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
                "OPENAI_IMAGE_API_KEY": "openai-image-test",
                "OPENAI_BASE_URL": "https://openai.test/v1",
                "PAYMENT_PROVIDER": "yookassa",
                "YOOKASSA_SHOP_ID": "shop-test",
                "YOOKASSA_SECRET_KEY": "secret-test",
                "YOOKASSA_WEBHOOK_PATH": "/yk/webhook",
                "YOOKASSA_RETURN_PATH": "/yk/return",
                "YOOKASSA_REQUEST_TIMEOUT_SECONDS": "9",
                "CEAI_ALLOW_EPHEMERAL_SQLITE": "true",
            },
        ):
            settings = load_settings()

        self.assertEqual(settings.ai_provider_mode, "real")
        self.assertEqual(settings.ai_request_timeout_seconds, 45)
        self.assertEqual(settings.deepseek_api_key, "deepseek-test")
        self.assertEqual(settings.deepseek_base_url, "https://deepseek.test")
        self.assertEqual(settings.openai_api_key, "openai-test")
        self.assertEqual(settings.openai_image_api_key, "openai-image-test")
        self.assertEqual(settings.openai_base_url, "https://openai.test/v1")
        self.assertEqual(settings.payment_provider, "yookassa")
        self.assertEqual(settings.yookassa_shop_id, "shop-test")
        self.assertEqual(settings.yookassa_secret_key, "secret-test")
        self.assertEqual(settings.yookassa_webhook_path, "/yk/webhook")
        self.assertEqual(settings.yookassa_return_path, "/yk/return")
        self.assertEqual(settings.yookassa_request_timeout_seconds, 9)
        self.assertTrue(settings.allow_ephemeral_sqlite)

    def test_seed_openai_models_are_configured_for_real_api(self) -> None:
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
                image = conn.execute(
                    "SELECT * FROM model_prices WHERE provider = ? AND model_key = ?",
                    ("openai", "gpt-image-2-medium"),
                ).fetchone()
                costs = {
                    row["model_key"]: row["coins_cost"]
                    for row in conn.execute(
                        "SELECT model_key, coins_cost FROM model_prices"
                    )
                }

            self.assertEqual(
                loads_dict(deepseek["config"])["api_model"], "deepseek-v4-flash"
            )
            self.assertEqual(
                loads_dict(deepseek["config"])["thinking_type"], "disabled"
            )
            self.assertEqual(openai["display_name"], "ChatGPT GPT-5.5")
            self.assertEqual(openai["coins_cost"], 3)
            self.assertEqual(
                costs,
                {
                    "deepseek-v4-flash": 1,
                    "gpt-4o-mini": 3,
                    "gpt-image-2-medium": 2,
                    "kling-3": 25,
                    "elevenlabs-tts": 5,
                },
            )
            self.assertEqual(loads_dict(openai["config"])["api_model"], "gpt-5.5")
            self.assertEqual(loads_dict(openai["config"])["reasoning_effort"], "low")
            image_config = loads_dict(image["config"])
            self.assertEqual(image["display_name"], "GPT Image 2")
            self.assertEqual(image["coins_cost"], 2)
            self.assertEqual(image_config["api_model"], "gpt-image-2")
            self.assertEqual(image_config["quality"], "medium")
            self.assertEqual(image_config["size"], "1024x1024")
            self.assertEqual(image_config["output_format"], "png")
            self.assertEqual(image_config["four_k_coins_cost"], 3)
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

    def test_openai_image_does_not_fall_back_to_mock_without_key(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://mock-payments.test/pay",
            ai_provider_mode="auto",
        )
        router = AIProviderRouter(settings)

        with self.assertRaisesRegex(ProviderError, "OPENAI_IMAGE_API_KEY"):
            router.generate(
                model={
                    "provider": "openai",
                    "model_key": "gpt-image-2-medium",
                    "display_name": "GPT Image 2",
                    "generation_type": "image",
                    "config": "{}",
                },
                prompt_text="Нарисуй кота",
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
                repo.upsert(
                    conn,
                    key="OPENAI_IMAGE_API_KEY",
                    value="saved-openai-image-key",
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
            self.assertIsNotNone(router.openai_image)
            self.assertEqual(router.openai_image.api_key, "saved-openai-image-key")
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
            self.assertIsNone(router.openai_image)

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
            self.assertIsNotNone(router.openai_image)
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
                            "OPENAI_IMAGE_API_KEY": "openai-image-test",
                        }
                    }
                ).encode("utf-8"),
            )

            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertTrue(loads(body)["ok"])
            with db.transaction() as conn:
                saved = AppSettingsRepository().get_many(
                    conn,
                    (
                        "DEEPSEEK_API_KEY",
                        "OPENAI_API_KEY",
                        "OPENAI_IMAGE_API_KEY",
                    ),
                )
            self.assertEqual(saved["DEEPSEEK_API_KEY"], "deepseek-test")
            self.assertEqual(saved["OPENAI_API_KEY"], "openai-test")
            self.assertEqual(saved["OPENAI_IMAGE_API_KEY"], "openai-image-test")
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
