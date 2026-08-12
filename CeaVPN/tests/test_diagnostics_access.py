from __future__ import annotations

from types import SimpleNamespace
import unittest

from aiohttp import web

from ceavpn.config import Settings
from ceavpn.main import _require_diagnostics_access


class DiagnosticsAccessTest(unittest.TestCase):
    def _request(self, *, token: str, supplied: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            app={
                "settings": Settings(
                    telegram_bot_token="bot-token",
                    database_url="sqlite:///:memory:",
                    app_env="test",
                    mock_payment_base_url="https://payments.example.test",
                    diagnostics_token=token,
                )
            },
            headers={"X-CEA-Diagnostics-Token": supplied} if supplied else {},
        )

    def test_diagnostics_are_hidden_without_a_configured_token(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            _require_diagnostics_access(self._request(token=""))

    def test_diagnostics_are_hidden_for_an_invalid_token(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            _require_diagnostics_access(
                self._request(token="diagnostics-secret", supplied="wrong")
            )

    def test_diagnostics_allow_the_exact_configured_token(self) -> None:
        _require_diagnostics_access(
            self._request(token="diagnostics-secret", supplied="diagnostics-secret")
        )
