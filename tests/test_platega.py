from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from ceai.services.platega import (
    PLATEGA_CONFIRMED,
    PLATEGA_PENDING,
    PlategaAuthenticationError,
    PlategaCallbackAuthenticationError,
    PlategaClient,
    PlategaConfigurationError,
    PlategaRequestError,
    PlategaResponseError,
)


TRANSACTION_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
OTHER_TRANSACTION_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
MERCHANT_ID = "merchant-123"
SECRET = "private-secret-value"


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def json_response(payload: object) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def created_payment_response(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "transactionId": TRANSACTION_ID,
        "status": "PENDING",
        "url": f"https://pay.platega.io/?id={TRANSACTION_ID}",
        "expiresIn": "00:15:00",
        "rate": 91.2,
    }
    result.update(overrides)
    return result


def transaction_response(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": TRANSACTION_ID,
        "status": "CONFIRMED",
        "paymentDetails": {"amount": 189, "currency": "RUB"},
        "paymentMethod": "SBPQR",
    }
    result.update(overrides)
    return result


class PlategaClientTest(unittest.TestCase):
    def make_client(self, *, timeout_seconds: float = 30) -> PlategaClient:
        return PlategaClient(
            MERCHANT_ID,
            SECRET,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def set_response(client: PlategaClient, payload: object) -> Mock:
        mocked = Mock(return_value=json_response(payload))
        client._opener.open = mocked
        return mocked

    def test_create_payment_uses_universal_endpoint_and_expected_contract(self) -> None:
        client = self.make_client(timeout_seconds=17)
        open_request = self.set_response(client, created_payment_response())

        result = client.create_payment(
            amount_rub=189,
            description="VPN на 1 месяц",
            return_url="https://example.test/payments/return",
            failed_url="https://example.test/payments/failed",
            payload="vpn-payment:42",
            user_id=100500,
            user_name="@customer",
        )

        self.assertEqual(result.transaction_id, TRANSACTION_ID)
        self.assertEqual(result.status, PLATEGA_PENDING)
        self.assertEqual(
            result.payment_url,
            f"https://pay.platega.io/?id={TRANSACTION_ID}",
        )
        self.assertEqual(result.expires_in, "00:15:00")

        request = open_request.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://app.platega.io/v2/transaction/process",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(open_request.call_args.kwargs["timeout"], 17.0)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-merchantid"], MERCHANT_ID)
        self.assertEqual(headers["x-secret"], SECRET)
        self.assertEqual(headers["content-type"], "application/json")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["paymentDetails"], {"amount": 189, "currency": "RUB"})
        self.assertEqual(
            body["metadata"],
            {"userId": "100500", "userName": "@customer"},
        )
        self.assertEqual(body["payload"], "vpn-payment:42")
        self.assertNotIn("id", body)

    def test_create_payment_omits_empty_user_name(self) -> None:
        client = self.make_client()
        open_request = self.set_response(client, created_payment_response())

        client.create_payment(
            amount_rub=399,
            description="VPN",
            return_url="https://example.test/return",
            failed_url="https://example.test/fail",
            payload="vpn-payment:43",
            user_id="200",
        )

        request = open_request.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["metadata"], {"userId": "200"})

    def test_get_transaction_validates_and_returns_payment_facts(self) -> None:
        client = self.make_client()
        open_request = self.set_response(
            client,
            transaction_response(paymentMethod=2),
        )

        result = client.get_transaction(TRANSACTION_ID)

        self.assertEqual(result.transaction_id, TRANSACTION_ID)
        self.assertEqual(result.status, PLATEGA_CONFIRMED)
        self.assertEqual(result.amount_rub, 189)
        self.assertEqual(result.currency, "RUB")
        self.assertEqual(result.payment_method, 2)
        request = open_request.call_args.args[0]
        self.assertEqual(
            request.full_url,
            f"https://app.platega.io/transaction/{TRANSACTION_ID}",
        )
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)

    def test_get_transaction_rejects_a_different_returned_id(self) -> None:
        client = self.make_client()
        self.set_response(
            client,
            transaction_response(id=OTHER_TRANSACTION_ID),
        )

        with self.assertRaisesRegex(PlategaResponseError, "different transaction"):
            client.get_transaction(TRANSACTION_ID)

    def test_created_payment_rejects_invalid_identity_status_and_url(self) -> None:
        invalid_responses = (
            created_payment_response(transactionId="not-a-uuid"),
            created_payment_response(transactionId=TRANSACTION_ID.upper()),
            created_payment_response(status="CONFIRMED"),
            created_payment_response(status="EXPIRED"),
            created_payment_response(url="http://pay.platega.io/pay"),
            created_payment_response(url="https://evil.example/pay"),
            created_payment_response(url="https://pay.platega.io.evil.example/pay"),
            created_payment_response(url="https://user@pay.platega.io/pay"),
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                client = self.make_client()
                self.set_response(client, response)
                with self.assertRaises(PlategaResponseError):
                    client.create_payment(
                        amount_rub=189,
                        description="VPN",
                        return_url="https://example.test/return",
                        failed_url="https://example.test/fail",
                        payload="vpn-payment:44",
                        user_id=1,
                    )

    def test_transaction_rejects_unknown_status_and_invalid_money_shapes(self) -> None:
        invalid_responses = (
            transaction_response(status="EXPIRED"),
            transaction_response(paymentDetails=None),
            transaction_response(paymentDetails={"amount": True, "currency": "RUB"}),
            transaction_response(paymentDetails={"amount": "189", "currency": "RUB"}),
            transaction_response(paymentDetails={"amount": 189.5, "currency": "RUB"}),
            transaction_response(paymentDetails={"amount": 0, "currency": "RUB"}),
            transaction_response(paymentDetails={"amount": 189, "currency": "rub"}),
            transaction_response(paymentDetails={"amount": 189, "currency": 643}),
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                client = self.make_client()
                self.set_response(client, response)
                with self.assertRaises(PlategaResponseError):
                    client.get_transaction(TRANSACTION_ID)

    def test_integral_numeric_api_amount_is_accepted_without_float_comparison(self) -> None:
        client = self.make_client()
        self.set_response(
            client,
            transaction_response(
                paymentDetails={"amount": 189.0, "currency": "RUB"}
            ),
        )
        self.assertEqual(client.get_transaction(TRANSACTION_ID).amount_rub, 189)

    def test_callback_authentication_is_case_insensitive_for_header_names(self) -> None:
        client = self.make_client()
        headers = {
            "x-MERCHANTid": MERCHANT_ID,
            "X-SECRET": SECRET,
        }
        self.assertTrue(client.is_authenticated_callback(headers))
        client.authenticate_callback(headers)

    def test_callback_authentication_compares_both_headers_and_rejects_mismatch(self) -> None:
        client = self.make_client()
        with patch("ceai.services.platega.hmac.compare_digest", return_value=False) as compare:
            self.assertFalse(
                client.is_authenticated_callback(
                    {"X-MerchantId": "wrong", "X-Secret": "also-wrong"}
                )
            )
        self.assertEqual(compare.call_count, 2)

        with self.assertRaises(PlategaCallbackAuthenticationError) as raised:
            client.authenticate_callback(
                {"X-MerchantId": MERCHANT_ID, "X-Secret": "wrong"}
            )
        self.assertNotIn(SECRET, str(raised.exception))

        self.assertFalse(
            client.is_authenticated_callback(
                {
                    "X-MerchantId": MERCHANT_ID,
                    "X-Secret": SECRET,
                    "x-secret": SECRET,
                }
            )
        )

    def test_credentials_and_client_configuration_are_validated(self) -> None:
        invalid_arguments = (
            ("", SECRET, {}),
            (MERCHANT_ID, "", {}),
            (MERCHANT_ID, f" {SECRET}", {}),
            (MERCHANT_ID, SECRET, {"api_base_url": "http://app.platega.io"}),
            (MERCHANT_ID, SECRET, {"api_base_url": "https://evil.example"}),
            (MERCHANT_ID, SECRET, {"timeout_seconds": 0}),
            (MERCHANT_ID, SECRET, {"timeout_seconds": 121}),
            (MERCHANT_ID, SECRET, {"timeout_seconds": float("nan")}),
        )
        for merchant_id, secret, kwargs in invalid_arguments:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PlategaConfigurationError) as raised:
                    PlategaClient(merchant_id, secret, **kwargs)
                self.assertNotIn(SECRET, str(raised.exception))

    def test_input_payload_is_validated_before_network_call(self) -> None:
        invalid_kwargs = (
            {"amount_rub": True},
            {"amount_rub": 0},
            {"amount_rub": 189.0},
            {"description": ""},
            {"payload": ""},
            {"user_id": None},
            {"return_url": "http://example.test/return"},
            {"failed_url": "https://user:pass@example.test/fail"},
        )
        baseline: dict[str, object] = {
            "amount_rub": 189,
            "description": "VPN",
            "return_url": "https://example.test/return",
            "failed_url": "https://example.test/fail",
            "payload": "vpn-payment:45",
            "user_id": 1,
        }
        for overrides in invalid_kwargs:
            with self.subTest(overrides=overrides):
                client = self.make_client()
                client._opener.open = Mock()
                with self.assertRaises(PlategaConfigurationError):
                    client.create_payment(**{**baseline, **overrides})
                client._opener.open.assert_not_called()

    def test_http_and_network_errors_do_not_expose_secret_or_error_body(self) -> None:
        client = self.make_client()
        error = urllib.error.HTTPError(
            "https://app.platega.io/v2/transaction/process",
            401,
            "unauthorized",
            {},
            io.BytesIO(f"bad {SECRET}".encode("utf-8")),
        )
        client._opener.open = Mock(side_effect=error)
        with self.assertRaises(PlategaAuthenticationError) as raised:
            client.create_payment(
                amount_rub=189,
                description="VPN",
                return_url="https://example.test/return",
                failed_url="https://example.test/fail",
                payload="vpn-payment:46",
                user_id=1,
            )
        self.assertNotIn(SECRET, str(raised.exception))

        client._opener.open = Mock(side_effect=TimeoutError(f"timeout {SECRET}"))
        with self.assertRaises(PlategaRequestError) as raised:
            client.get_transaction(TRANSACTION_ID)
        self.assertNotIn(SECRET, str(raised.exception))
        client._opener.open.assert_called_once()

    def test_invalid_or_oversized_json_is_rejected(self) -> None:
        invalid_bodies = (
            b"not json",
            b"[]",
            b"\xff",
            b'{"id":"first","id":"second"}',
            b'{"value":NaN}',
            b"{" + (b" " * (1024 * 1024)),
        )
        for body in invalid_bodies:
            with self.subTest(body_size=len(body)):
                client = self.make_client()
                client._opener.open = Mock(return_value=FakeResponse(body))
                with self.assertRaises(PlategaResponseError):
                    client.get_transaction(TRANSACTION_ID)

    def test_http_404_is_a_safe_request_error(self) -> None:
        client = self.make_client()
        error = urllib.error.HTTPError(
            f"https://app.platega.io/transaction/{TRANSACTION_ID}",
            404,
            "not found",
            {},
            io.BytesIO(b"internal details"),
        )
        client._opener.open = Mock(side_effect=error)
        with self.assertRaisesRegex(PlategaRequestError, "not found"):
            client.get_transaction(TRANSACTION_ID)


if __name__ == "__main__":
    unittest.main()
