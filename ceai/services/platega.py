from __future__ import annotations

import hmac
import http.client
import json
import math
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlsplit


PLATEGA_API_BASE_URL = "https://app.platega.io"
PLATEGA_API_HOST = "app.platega.io"
PLATEGA_PAYMENT_HOSTS = frozenset({"pay.platega.io"})
PLATEGA_STATUSES = frozenset(
    {"PENDING", "CONFIRMED", "CANCELED", "CHARGEBACKED"}
)
PLATEGA_PENDING = "PENDING"
PLATEGA_CONFIRMED = "CONFIRMED"
PLATEGA_CANCELED = "CANCELED"
PLATEGA_CHARGEBACKED = "CHARGEBACKED"

_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TEXT_FIELD_LENGTH = 4096
_MAX_HEADER_LENGTH = 4096
_MAX_PAYMENT_AMOUNT_RUB = 10**12


class PlategaError(RuntimeError):
    """Base class for safe-to-surface Platega integration errors."""


class PlategaConfigurationError(PlategaError):
    """The merchant credentials or client configuration are invalid."""


class PlategaRequestError(PlategaError):
    """The Platega API could not complete a request."""


class PlategaAuthenticationError(PlategaRequestError):
    """Platega rejected the configured API credentials."""


class PlategaResponseError(PlategaError):
    """Platega returned a response that is unsafe or cannot be verified."""


class PlategaCallbackAuthenticationError(PlategaError):
    """A callback does not carry the expected merchant credentials."""


@dataclass(frozen=True)
class PlategaCreatedPayment:
    transaction_id: str
    status: str
    payment_url: str
    expires_in: str | None


@dataclass(frozen=True)
class PlategaTransaction:
    transaction_id: str
    status: str
    amount_rub: int
    currency: str
    payment_method: str | int | None


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward merchant credentials to a redirected origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class PlategaClient:
    def __init__(
        self,
        merchant_id: str,
        secret: str,
        *,
        api_base_url: str = PLATEGA_API_BASE_URL,
        timeout_seconds: float = 30,
    ) -> None:
        self.merchant_id = _validate_header_credential(
            merchant_id, field_name="merchant id"
        )
        self._secret = _validate_header_credential(secret, field_name="secret")
        self.api_base_url = _validate_api_base_url(api_base_url)
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self._opener = urllib.request.build_opener(_RejectRedirectHandler())

    def create_payment(
        self,
        *,
        amount_rub: int,
        description: str,
        return_url: str,
        failed_url: str,
        payload: str,
        user_id: int | str,
        user_name: str = "",
    ) -> PlategaCreatedPayment:
        amount = _validate_input_amount(amount_rub)
        description_value = _validate_text(
            description,
            field_name="description",
            required=True,
        )
        payload_value = _validate_text(
            payload,
            field_name="payload",
            required=True,
        )
        if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
            raise PlategaConfigurationError("Platega user id is invalid.")
        user_id_value = _validate_text(
            str(user_id),
            field_name="user id",
            required=True,
            max_length=256,
        )
        user_name_value = _validate_text(
            user_name,
            field_name="user name",
            required=False,
            max_length=256,
        )
        metadata = {"userId": user_id_value}
        if user_name_value:
            metadata["userName"] = user_name_value

        response = self._request_json(
            "POST",
            "/v2/transaction/process",
            payload={
                "paymentDetails": {"amount": amount, "currency": "RUB"},
                "description": description_value,
                "return": _validate_public_https_url(
                    return_url, field_name="return URL"
                ),
                "failedUrl": _validate_public_https_url(
                    failed_url, field_name="failed URL"
                ),
                "payload": payload_value,
                "metadata": metadata,
            },
        )

        transaction_id = _validate_transaction_id(
            response.get("transactionId"), field_name="transactionId"
        )
        status = _validate_status(response.get("status"))
        if status != PLATEGA_PENDING:
            raise PlategaResponseError(
                "Platega returned an unexpected status for a new payment."
            )
        payment_url = _validate_payment_url(response.get("url"))
        expires_in = _validate_optional_response_text(
            response.get("expiresIn"), field_name="expiresIn"
        )
        return PlategaCreatedPayment(
            transaction_id=transaction_id,
            status=status,
            payment_url=payment_url,
            expires_in=expires_in,
        )

    def get_transaction(self, transaction_id: str) -> PlategaTransaction:
        expected_id = _validate_transaction_id(
            transaction_id, field_name="transaction id"
        )
        response = self._request_json("GET", f"/transaction/{expected_id}")
        returned_id = _validate_transaction_id(
            response.get("id"), field_name="transaction id"
        )
        if not hmac.compare_digest(returned_id, expected_id):
            raise PlategaResponseError(
                "Platega returned details for a different transaction."
            )

        status = _validate_status(response.get("status"))
        payment_details = response.get("paymentDetails")
        if not isinstance(payment_details, dict):
            raise PlategaResponseError(
                "Platega returned invalid payment details."
            )
        amount_rub = _validate_response_amount(payment_details.get("amount"))
        currency = _validate_currency(payment_details.get("currency"))
        payment_method = _validate_payment_method(response.get("paymentMethod"))
        return PlategaTransaction(
            transaction_id=returned_id,
            status=status,
            amount_rub=amount_rub,
            currency=currency,
            payment_method=payment_method,
        )

    def is_authenticated_callback(self, headers: Mapping[str, str]) -> bool:
        normalized_headers: dict[str, str] = {}
        duplicate_auth_header = False
        for key, value in headers.items():
            if isinstance(key, str) and isinstance(value, str):
                normalized_key = key.lower()
                if normalized_key in {"x-merchantid", "x-secret"}:
                    if normalized_key in normalized_headers:
                        duplicate_auth_header = True
                    normalized_headers[normalized_key] = value

        received_merchant_id = normalized_headers.get("x-merchantid", "")
        received_secret = normalized_headers.get("x-secret", "")

        # Evaluate both comparisons even when the first header is wrong or absent.
        merchant_matches = hmac.compare_digest(
            self.merchant_id.encode("utf-8"),
            received_merchant_id.encode("utf-8"),
        )
        secret_matches = hmac.compare_digest(
            self._secret.encode("utf-8"),
            received_secret.encode("utf-8"),
        )
        return bool(merchant_matches & secret_matches) and not duplicate_auth_header

    def authenticate_callback(self, headers: Mapping[str, str]) -> None:
        if not self.is_authenticated_callback(headers):
            raise PlategaCallbackAuthenticationError(
                "Platega callback authentication failed."
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "X-MerchantId": self.merchant_id,
            "X-Secret": self._secret,
        }
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise PlategaConfigurationError(
                    "The Platega request contains invalid data."
                ) from exc
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.api_base_url + "/" + path.lstrip("/"),
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self._opener.open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw_body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            finally:
                if exc.code in {401, 403}:
                    raise PlategaAuthenticationError(
                        "Platega rejected the configured API credentials."
                    ) from None
                if exc.code == 404:
                    raise PlategaRequestError(
                        "The Platega transaction was not found."
                    ) from None
                raise PlategaRequestError(
                    f"Platega returned HTTP {int(exc.code)}."
                ) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            http.client.HTTPException,
            OSError,
        ):
            raise PlategaRequestError(
                "Platega is temporarily unavailable."
            ) from None

        if not isinstance(raw_body, bytes):
            raise PlategaResponseError("Platega returned an invalid response body.")
        if len(raw_body) > _MAX_RESPONSE_BYTES:
            raise PlategaResponseError("Platega returned an oversized response.")
        try:
            decoded = raw_body.decode("utf-8")
            parsed = json.loads(
                decoded,
                parse_int=Decimal,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise PlategaResponseError("Platega returned invalid JSON.") from None
        if not isinstance(parsed, dict):
            raise PlategaResponseError("Platega returned an invalid JSON object.")
        return parsed


def _validate_header_credential(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise PlategaConfigurationError(f"Platega {field_name} must be text.")
    if not value or value != value.strip():
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.")
    if len(value) > _MAX_HEADER_LENGTH or _contains_control_character(value):
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise PlategaConfigurationError(
            f"Platega {field_name} must contain ASCII characters only."
        ) from None
    return value


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlategaConfigurationError("Platega timeout must be a number.")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
        raise PlategaConfigurationError(
            "Platega timeout must be greater than 0 and at most 120 seconds."
        )
    return timeout


def _validate_api_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise PlategaConfigurationError("Platega API URL must be text.")
    if value != value.strip() or _contains_control_character(value):
        raise PlategaConfigurationError("Platega API URL is invalid.")
    cleaned = value.rstrip("/")
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise PlategaConfigurationError("Platega API URL is invalid.") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != PLATEGA_API_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PlategaConfigurationError(
            "Platega API URL must use the official HTTPS endpoint."
        )
    return cleaned


def _validate_input_amount(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_PAYMENT_AMOUNT_RUB
    ):
        raise PlategaConfigurationError(
            "Platega amount must be a positive integer number of rubles."
        )
    return value


def _validate_response_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PlategaResponseError("Platega returned an invalid payment amount.")
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise PlategaResponseError(
            "Platega returned an invalid payment amount."
        ) from exc
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > _MAX_PAYMENT_AMOUNT_RUB
        or amount != amount.to_integral_value()
    ):
        raise PlategaResponseError("Platega returned an invalid payment amount.")
    return int(amount)


def _validate_currency(value: Any) -> str:
    if value != "RUB" or not isinstance(value, str):
        raise PlategaResponseError("Platega returned an invalid payment currency.")
    return value


def _validate_transaction_id(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise PlategaResponseError(f"Platega returned an invalid {field_name}.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise PlategaResponseError(
            f"Platega returned an invalid {field_name}."
        ) from exc
    canonical = str(parsed)
    if value != canonical:
        raise PlategaResponseError(f"Platega returned an invalid {field_name}.")
    return canonical


def _validate_status(value: Any) -> str:
    if not isinstance(value, str) or value not in PLATEGA_STATUSES:
        raise PlategaResponseError("Platega returned an unknown payment status.")
    return value


def _validate_payment_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > _MAX_TEXT_FIELD_LENGTH
    ):
        raise PlategaResponseError("Platega returned an invalid payment URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PlategaResponseError("Platega returned an invalid payment URL.") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in PLATEGA_PAYMENT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or _contains_control_character(value)
    ):
        raise PlategaResponseError("Platega returned an untrusted payment URL.")
    return value


def _validate_public_https_url(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or _contains_control_character(value)
    ):
        raise PlategaConfigurationError(
            f"Platega {field_name} must be a public HTTPS URL."
        )
    return value


def _validate_text(
    value: str,
    *,
    field_name: str,
    required: bool,
    max_length: int = _MAX_TEXT_FIELD_LENGTH,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.")
    if required and not value:
        raise PlategaConfigurationError(f"Platega {field_name} is required.")
    if len(value) > max_length or _contains_control_character(value):
        raise PlategaConfigurationError(f"Platega {field_name} is invalid.")
    return value


def _validate_optional_response_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or _contains_control_character(value)
    ):
        raise PlategaResponseError(f"Platega returned an invalid {field_name}.")
    return value


def _validate_payment_method(value: Any) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PlategaResponseError("Platega returned an invalid payment method.")
    if isinstance(value, int) and 0 <= value <= 2**31 - 1:
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and 0 <= value <= 2**31 - 1
        and value == value.to_integral_value()
    ):
        return int(value)
    if (
        isinstance(value, str)
        and value
        and value == value.strip()
        and len(value) <= 128
        and not _contains_control_character(value)
    ):
        return value
    raise PlategaResponseError("Platega returned an invalid payment method.")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


__all__ = [
    "PLATEGA_API_BASE_URL",
    "PLATEGA_CANCELED",
    "PLATEGA_CHARGEBACKED",
    "PLATEGA_CONFIRMED",
    "PLATEGA_PENDING",
    "PLATEGA_STATUSES",
    "PlategaAuthenticationError",
    "PlategaCallbackAuthenticationError",
    "PlategaClient",
    "PlategaConfigurationError",
    "PlategaCreatedPayment",
    "PlategaError",
    "PlategaRequestError",
    "PlategaResponseError",
    "PlategaTransaction",
]
