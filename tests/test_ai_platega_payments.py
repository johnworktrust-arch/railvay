from __future__ import annotations

import unittest
import uuid

from ceai.database import Database
from ceai.seed import seed_reference_data
from ceai.services.payments import PaymentService
from ceai.services.platega import (
    PLATEGA_CONFIRMED,
    PLATEGA_PENDING,
    PlategaCallbackAuthenticationError,
    PlategaCreatedPayment,
    PlategaTransaction,
)
from ceai.services.users import UserService


class FakePlategaClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.transactions: dict[str, PlategaTransaction] = {}

    def create_payment(self, **kwargs) -> PlategaCreatedPayment:
        transaction_id = str(uuid.uuid4())
        self.created.append(dict(kwargs))
        self.transactions[transaction_id] = PlategaTransaction(
            transaction_id=transaction_id,
            status=PLATEGA_PENDING,
            amount_rub=int(kwargs["amount_rub"]),
            currency="RUB",
            payment_method=None,
        )
        return PlategaCreatedPayment(
            transaction_id=transaction_id,
            status=PLATEGA_PENDING,
            payment_url=f"https://pay.platega.io/{transaction_id}",
            expires_in="00:15:00",
        )

    def get_transaction(self, transaction_id: str) -> PlategaTransaction:
        return self.transactions[transaction_id]

    def authenticate_callback(self, headers) -> None:
        if (
            headers.get("X-MerchantId") != "merchant"
            or headers.get("X-Secret") != "secret"
        ):
            raise PlategaCallbackAuthenticationError("invalid callback")


class AiPlategaPaymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database("sqlite:///:memory:")
        self.db.migrate()
        seed_reference_data(self.db)
        self.user = UserService(self.db).ensure_telegram_user(
            telegram_id=991001,
            username="ai_platega_user",
            first_name="AI",
            last_name="Buyer",
            language_code="ru",
        )
        self.client = FakePlategaClient()
        self.payments = PaymentService(
            self.db,
            mock_payment_base_url="https://mock.example.test",
            payment_provider="platega",
            app_base_url="https://bot.example.test",
            platega_client=self.client,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.db.close()

    def _create(self) -> dict:
        return self.payments.create_payment(
            user_id=int(self.user["id"]),
            plan_code="start",
            payment_method="card_sbp",
        )

    def test_card_sbp_creates_platega_checkout_for_ai_plan(self) -> None:
        payment = self._create()

        self.assertEqual(payment["provider"], "platega")
        self.assertEqual(payment["status"], "pending")
        self.assertTrue(payment["payment_url"].startswith("https://pay.platega.io/"))
        request = self.client.created[0]
        self.assertEqual(request["return_url"], "https://bot.example.test/payments/platega/return")
        self.assertEqual(request["failed_url"], "https://bot.example.test/payments/platega/failed")
        self.assertTrue(str(request["payload"]).startswith("ceaai:"))

    def test_confirmed_callback_credits_subscription_once(self) -> None:
        payment = self._create()
        current = self.client.transactions[payment["external_id"]]
        self.client.transactions[payment["external_id"]] = PlategaTransaction(
            transaction_id=current.transaction_id,
            status=PLATEGA_CONFIRMED,
            amount_rub=current.amount_rub,
            currency="RUB",
            payment_method=2,
        )
        payload = {"id": payment["external_id"], "status": "CONFIRMED"}
        headers = {"X-MerchantId": "merchant", "X-Secret": "secret"}

        first = self.payments.process_platega_webhook(
            headers=headers, payload=payload
        )
        duplicate = self.payments.process_platega_webhook(
            headers=headers, payload=payload
        )

        self.assertTrue(first.processed)
        self.assertGreater(first.credited_coins, 0)
        self.assertTrue(duplicate.duplicate)
        with self.db.transaction() as conn:
            credits = conn.execute(
                "SELECT COUNT(*) AS count FROM coin_transactions WHERE payment_id = ?",
                (payment["id"],),
            ).fetchone()["count"]
        self.assertEqual(credits, 1)

    def test_amount_mismatch_never_credits_coins(self) -> None:
        payment = self._create()
        current = self.client.transactions[payment["external_id"]]
        self.client.transactions[payment["external_id"]] = PlategaTransaction(
            transaction_id=current.transaction_id,
            status=PLATEGA_CONFIRMED,
            amount_rub=current.amount_rub + 1,
            currency="RUB",
            payment_method=2,
        )

        with self.assertRaisesRegex(Exception, "Сумма или валюта"):
            self.payments.process_platega_webhook(
                headers={"X-MerchantId": "merchant", "X-Secret": "secret"},
                payload={"id": payment["external_id"], "status": "CONFIRMED"},
            )

        with self.db.transaction() as conn:
            stored = conn.execute(
                "SELECT status FROM payments WHERE id = ?", (payment["id"],)
            ).fetchone()
            credits = conn.execute(
                "SELECT COUNT(*) AS count FROM coin_transactions WHERE payment_id = ?",
                (payment["id"],),
            ).fetchone()["count"]
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(credits, 0)

    def test_invalid_callback_authentication_is_rejected(self) -> None:
        payment = self._create()
        with self.assertRaises(PlategaCallbackAuthenticationError):
            self.payments.process_platega_webhook(
                headers={"X-MerchantId": "merchant", "X-Secret": "wrong"},
                payload={"id": payment["external_id"]},
            )


if __name__ == "__main__":
    unittest.main()
