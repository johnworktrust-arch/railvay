from __future__ import annotations

import base64
import hashlib
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ceavpn.config import Settings
from ceavpn.repositories.vpn_subscriptions import VpnSubscriptionRepository
from ceavpn.repositories.vpn_subscription_devices import VpnSubscriptionDeviceRepository
from ceavpn.vpn_subscription_delivery import (
    _device_metadata,
    _landing_html,
    delivery_subscription_url,
    happ_auto_selection_headers,
    merge_subscription_profiles,
    parse_extra_profiles,
    qualification_profile_fingerprint,
    qualified_extra_profiles,
    register_vpn_subscription_delivery_routes,
)

QUALIFICATION_URL = (
    "https://sub.edge.example.test:8443/"
    ".well-known/ceavpn-whitelist-status"
)


def _qualification_fingerprint(
    *,
    address: str = "edge.example.test",
    port: int = 443,
    transport: str = "xhttp",
    security: str = "reality",
    path: str = "/xhttp",
    sni: str = "cover.example.test",
    pbk: str = "A" * 43,
    sid: str = "0011",
    fingerprint: str = "chrome",
    qualification_url: str = QUALIFICATION_URL,
    server_code: str = "ru-wl-1",
) -> str:
    payload = {
        "address": address,
        "port": port,
        "transport": transport,
        "security": security,
        "path": path,
        "sni": sni,
        "pbk": pbk,
        "sid": sid,
        "fingerprint": fingerprint,
        "qualification_url": qualification_url,
        "server_code": server_code,
        "mode": "auto",
        "extra": {
            "scMaxEachPostBytes": 1000000,
            "scMaxConcurrentPosts": 100,
            "scMinPostsIntervalMs": 30,
            "xPaddingBytes": "100-1000",
            "noGRPCHeader": False,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()


QUALIFICATION_FINGERPRINT = _qualification_fingerprint()


class VpnSubscriptionDeliveryTest(unittest.TestCase):
    def test_happ_auto_selection_uses_native_lowest_delay_mode(self) -> None:
        self.assertEqual(
            happ_auto_selection_headers("cea_provider_2026"),
            {
                "providerid": "cea_provider_2026",
                "subscription-autoconnect": "1",
                "subscription-autoconnect-type": "lowestdelay",
                "subscription-ping-onopen-enabled": "1",
            },
        )

    def test_happ_auto_selection_is_disabled_without_valid_provider_id(self) -> None:
        self.assertEqual(happ_auto_selection_headers(""), {})
        self.assertEqual(happ_auto_selection_headers("line\nbreak"), {})

    def test_device_metadata_recognizes_iphone_model_identifier(self) -> None:
        request = type(
            "Request",
            (),
            {
                "headers": {
                    "User-Agent": "Happ/5.6.0/iOS/18.7.3/iPhone16,1",
                    "X-Device-ID": "device-15-pro-1234",
                },
                "remote": "203.0.113.10",
            },
        )()

        _, model, platform, _ = _device_metadata(request)

        self.assertEqual(model, "iPhone 15 Pro")
        self.assertEqual(platform, "iOS / 18.7.3")

    def test_happ_device_token_is_stable_when_ip_changes(self) -> None:
        def request(remote: str):
            return type(
                "Request",
                (),
                {
                    "headers": {
                        "User-Agent": "Happ/5.6.0/ios/2608171408651",
                    },
                    "remote": remote,
                },
            )()

        first = _device_metadata(request("203.0.113.10"))
        second = _device_metadata(request("198.51.100.20"))

        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], "iPhone")
        self.assertEqual(first[2], "iOS")

    def test_happ_device_token_distinguishes_devices(self) -> None:
        first = type(
            "Request",
            (),
            {
                "headers": {"User-Agent": "Happ/5.6.0/ios/2608171408651"},
                "remote": "203.0.113.10",
            },
        )()
        second = type(
            "Request",
            (),
            {
                "headers": {"User-Agent": "Happ/5.6.0/ios/2608171408551"},
                "remote": "203.0.113.10",
            },
        )()

        self.assertNotEqual(_device_metadata(first)[0], _device_metadata(second)[0])

    def test_android_user_agent_extracts_hardware_model(self) -> None:
        request = type(
            "Request",
            (),
            {
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 14; SM-S918B "
                        "Build/UP1A.231005.007)"
                    )
                },
                "remote": "203.0.113.10",
            },
        )()

        _, model, platform, _ = _device_metadata(request)

        self.assertEqual(model, "SM-S918B")
        self.assertEqual(platform, "Android / 14")

    def test_happ_landing_requires_an_explicit_button_press(self) -> None:
        html = _landing_html("https://bot.example.test/sub/token", client="connect")

        self.assertIn("Открыть Happ", html)
        self.assertNotIn("http-equiv=\"refresh\"", html)
        self.assertNotIn("window.location", html)
        self.assertIn("CEA VPN", html)
        self.assertIn("Как подключиться", html)
        self.assertIn("Выберите сервер и включите VPN", html)
        self.assertIn("background:#0b0c0e", html)

    def test_builds_opaque_railway_subscription_url(self) -> None:
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            app_base_url="https://bot.example.test",
            vpn_delivery_base_url="https://bot.example.test",
            vpn_delivery_signing_secret="s" * 48,
        )
        url = delivery_subscription_url(
            {
                "id": 42,
                "provider_username": "u_abcdef123456",
                "subscription_url": "https://origin.example.test/sub/private",
            },
            settings,
        )
        self.assertRegex(
            url,
            r"^https://bot\.example\.test/sub/42\.[0-9a-f]{64}$",
        )
        self.assertNotIn("private", url)
        self.assertNotIn("u_abcdef", url)

    def test_merges_extra_profile_into_base64_subscription(self) -> None:
        provider_uuid = "9c97ef67-c753-46d0-9529-74a33f566773"
        existing = (
            f"vless://{provider_uuid}@old.example.test:8443"
            "?encryption=none&security=tls&type=ws&path=%2Fws-"
            f"{'1' * 48}#Old\n"
        ).encode()
        encoded = base64.b64encode(existing)
        profiles = parse_extra_profiles(
            "[{"
            '"remark":"🇷🇺 Россия · Yandex",'
            '"address":"sub.example.test","port":8443,'
            '"sni":"sub.example.test","host":"sub.example.test",'
            f'"path":"/ws-{"2" * 48}"'
            "}]"
        )

        merged = merge_subscription_profiles(
            encoded,
            provider_uuid=provider_uuid,
            profiles=profiles,
        )
        decoded = base64.b64decode(merged).decode()
        self.assertEqual(decoded.count("vless://"), 2)
        self.assertIn("@sub.example.test:8443", decoded)
        self.assertIn("%F0%9F%87%B7%F0%9F%87%BA%20%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F", decoded)
        profile_uri = decoded.splitlines()[-1]
        query = parse_qs(urlsplit(profile_uri).query)
        self.assertEqual(query["type"], ["ws"])
        self.assertEqual(query["security"], ["tls"])
        self.assertEqual(query["host"], ["sub.example.test"])

        duplicate = merge_subscription_profiles(
            merged,
            provider_uuid=provider_uuid,
            profiles=profiles,
        )
        self.assertEqual(base64.b64decode(duplicate).decode().count("vless://"), 2)

    def test_rejects_non_ws_profile_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid VPN extra profile"):
            parse_extra_profiles(
                '[{"remark":"Bad","address":"a","port":443,'
                '"sni":"a","host":"a","path":"/"}]'
            )

    def test_merges_xhttp_reality_profile_without_flow(self) -> None:
        provider_uuid = "9c97ef67-c753-46d0-9529-74a33f566773"
        existing = (
            f"vless://{provider_uuid}@old.example.test:443"
            "?encryption=none&security=tls&type=ws&host=old.example.test"
            f"&path=%2Fws-{'1' * 48}#Old\n"
        ).encode()
        profiles = parse_extra_profiles(
            "[{"
            '"remark":"🇷🇺 LTE · XHTTP",'
            '"address":"edge.example.test","port":443,'
            '"transport":"xhttp","security":"reality",'
            '"path":"/transport/live","sni":"cover.example.test",'
            f'"pbk":"{"A" * 43}","sid":"A1B2C3D4",'
            '"fingerprint":"chrome",'
            f'"qualification_url":"{QUALIFICATION_URL}",'
            '"server_code":"ru-wl-1",'
            '"qualification_fingerprint":"'
            f'{_qualification_fingerprint(path="/transport/live", sid="a1b2c3d4")}"'
            "}]"
        )

        merged = merge_subscription_profiles(
            existing,
            provider_uuid=provider_uuid,
            profiles=profiles,
        ).decode()
        links = merged.splitlines()
        self.assertEqual(len(links), 2)
        parsed = urlsplit(links[-1])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "edge.example.test")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(unquote(parsed.fragment), "🇷🇺 LTE · XHTTP")
        self.assertEqual(
            query,
            {
                "encryption": ["none"],
                "type": ["xhttp"],
                "security": ["reality"],
                "path": ["/transport/live"],
                "sni": ["cover.example.test"],
                "fp": ["chrome"],
                "pbk": ["A" * 43],
                "sid": ["a1b2c3d4"],
                "mode": ["auto"],
                "extra": [
                    json.dumps(
                        {
                            "scMaxEachPostBytes": 1000000,
                            "scMaxConcurrentPosts": 100,
                            "scMinPostsIntervalMs": 30,
                            "xPaddingBytes": "100-1000",
                            "noGRPCHeader": False,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ],
            },
        )
        self.assertNotIn("flow", query)
        self.assertNotIn("host", query)
        self.assertIn("path=%2Ftransport%2Flive", links[-1])
        self.assertIn("headerType=", links[-1])
        self.assertNotIn("server_code", links[-1])

        duplicate = merge_subscription_profiles(
            merged.encode(),
            provider_uuid=provider_uuid,
            profiles=profiles,
        ).decode()
        self.assertEqual(len(duplicate.splitlines()), 2)

    def test_accepts_public_key_alias_for_xhttp_reality(self) -> None:
        profiles = parse_extra_profiles(
            "[{"
            '"remark":"Alias","address":"edge.example.test","port":443,'
            '"transport":"xhttp","security":"reality","path":"/xhttp",'
            '"sni":"cover.example.test",'
            f'"public_key":"{"B" * 43}","sid":"0011",'
            '"fingerprint":"firefox",'
            f'"qualification_url":"{QUALIFICATION_URL}",'
            '"server_code":"ru-wl-1",'
            '"qualification_fingerprint":"'
            f'{_qualification_fingerprint(pbk="B" * 43, fingerprint="firefox")}"'
            "}]"
        )

        self.assertEqual(profiles[0]["pbk"], "B" * 43)
        self.assertNotIn("public_key", profiles[0])
        self.assertEqual(
            qualification_profile_fingerprint(profiles[0]),
            _qualification_fingerprint(
                pbk="B" * 43,
                fingerprint="firefox",
            ),
        )

    def test_empty_configuration_does_not_publish_extra_profile(self) -> None:
        provider_uuid = "9c97ef67-c753-46d0-9529-74a33f566773"
        body = (
            f"vless://{provider_uuid}@old.example.test:443"
            "?encryption=none&security=tls&type=ws&path=%2Fold#Old\n"
        ).encode()

        profiles = parse_extra_profiles("[]")
        merged = merge_subscription_profiles(
            body,
            provider_uuid=provider_uuid,
            profiles=profiles,
        )

        self.assertEqual(profiles, ())
        self.assertEqual(merged, body)
        self.assertNotIn(b"type=xhttp", merged)

    def test_runbook_extra_profile_example_is_parseable(self) -> None:
        runbook = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "VPN_SERVER_RUNBOOK.md"
        ).read_text(encoding="utf-8")
        line = next(
            line
            for line in runbook.splitlines()
            if line.startswith("VPN_EXTRA_PROFILES_JSON=[")
        )

        profiles = parse_extra_profiles(line.split("=", 1)[1])

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["server_code"], "ru-wl-1")
        self.assertEqual(profiles[0]["address"], "192.0.2.10")

    def test_rejects_xhttp_secrets_and_unapproved_fields(self) -> None:
        base = (
            '{"remark":"Bad","address":"edge.example.test","port":443,'
            '"transport":"xhttp","security":"reality","path":"/xhttp",'
            '"sni":"cover.example.test",'
            f'"pbk":"{"A" * 43}","sid":"0011","fingerprint":"chrome",'
            f'"qualification_url":"{QUALIFICATION_URL}",'
            '"server_code":"ru-wl-1",'
            f'"qualification_fingerprint":"{QUALIFICATION_FINGERPRINT}"'
        )
        for extra in (
            ',"private_key":"secret"}',
            ',"password":"secret"}',
            ',"flow":"xtls-rprx-vision"}',
            ',"host":"edge.example.test"}',
            ',"mode":"auto"}',
            ',"extra":{}}',
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(
                    ValueError, "Invalid VPN extra profile"
                ):
                    parse_extra_profiles(f"[{base}{extra}]")

    def test_rejects_xhttp_control_characters_and_bad_urls(self) -> None:
        valid = {
            "remark": "Bad",
            "address": "edge.example.test",
            "port": 443,
            "transport": "xhttp",
            "security": "reality",
            "path": "/xhttp",
            "sni": "cover.example.test",
            "pbk": "A" * 43,
            "sid": "0011",
            "fingerprint": "chrome",
            "qualification_url": QUALIFICATION_URL,
            "qualification_fingerprint": QUALIFICATION_FINGERPRINT,
            "server_code": "ru-wl-1",
        }
        invalid_values = (
            ("remark", "Bad\nRemark"),
            ("address", "https://edge.example.test"),
            ("address", "user@edge.example.test"),
            ("sni", "https://cover.example.test"),
            ("path", "https://edge.example.test/xhttp"),
            ("path", "/xhttp?secret=value"),
            ("path", "/xhttp#fragment"),
            ("path", "/../xhttp"),
            ("transport", "xhttp\n"),
            ("security", "reality\u0000"),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                item = dict(valid)
                item[key] = value
                with self.assertRaisesRegex(
                    ValueError, "Invalid VPN extra profile"
                ):
                    parse_extra_profiles(json.dumps([item]))

    def test_rejects_incomplete_or_invalid_xhttp_reality_profile(self) -> None:
        valid = {
            "remark": "Bad",
            "address": "edge.example.test",
            "port": 443,
            "transport": "xhttp",
            "security": "reality",
            "path": "/xhttp",
            "sni": "cover.example.test",
            "pbk": "A" * 43,
            "sid": "0011",
            "fingerprint": "chrome",
            "qualification_url": QUALIFICATION_URL,
            "qualification_fingerprint": QUALIFICATION_FINGERPRINT,
            "server_code": "ru-wl-1",
        }
        for missing in (
            "address",
            "port",
            "remark",
            "transport",
            "security",
            "path",
            "sni",
            "pbk",
            "sid",
            "fingerprint",
            "qualification_url",
            "qualification_fingerprint",
            "server_code",
        ):
            with self.subTest(missing=missing):
                item = dict(valid)
                del item[missing]
                with self.assertRaisesRegex(
                    ValueError, "Invalid VPN extra profile"
                ):
                    parse_extra_profiles(json.dumps([item]))

        invalid_fields = (
            ("pbk", "not-a-public-key"),
            ("sid", "abc"),
            ("sid", "not-hex"),
            ("fingerprint", "unknown"),
            ("transport", "tcp"),
            ("security", "tls"),
            (
                "qualification_url",
                "https://other.example.test:443/.well-known/"
                "ceavpn-whitelist-status",
            ),
            (
                "qualification_url",
                "https://user@sub.edge.example.test:8443/.well-known/"
                "ceavpn-whitelist-status",
            ),
            ("qualification_url", f"{QUALIFICATION_URL}?leak=1"),
            ("qualification_fingerprint", "A" * 64),
            ("qualification_fingerprint", "a" * 63),
            ("server_code", "bad code"),
        )
        for key, value in invalid_fields:
            with self.subTest(key=key, value=value):
                item = dict(valid)
                item[key] = value
                with self.assertRaisesRegex(
                    ValueError, "Invalid VPN extra profile"
                ):
                    parse_extra_profiles(json.dumps([item]))

    def test_qualification_fingerprint_binds_public_profile_fields(
        self,
    ) -> None:
        valid = {
            "remark": "Candidate",
            "address": "edge.example.test",
            "port": 443,
            "transport": "xhttp",
            "security": "reality",
            "path": "/xhttp",
            "sni": "cover.example.test",
            "pbk": "A" * 43,
            "sid": "0011",
            "fingerprint": "chrome",
            "qualification_url": QUALIFICATION_URL,
            "qualification_fingerprint": QUALIFICATION_FINGERPRINT,
            "server_code": "ru-wl-1",
        }
        mutations = {
            "address": "other.example.test",
            "port": 8443,
            "transport": "ws",
            "security": "tls",
            "path": "/xhttp-other",
            "sni": "other.example.test",
            "pbk": "B" * 43,
            "sid": "0022",
            "fingerprint": "firefox",
            "qualification_url": (
                "https://other.example.test:8443/.well-known/"
                "ceavpn-whitelist-status"
            ),
            "server_code": "ru-wl-2",
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                item = dict(valid)
                item[key] = value
                with self.assertRaisesRegex(
                    ValueError, "Invalid VPN extra profile"
                ):
                    parse_extra_profiles(json.dumps([item]))

        changed_remark = dict(valid, remark="Renamed candidate")
        parsed = parse_extra_profiles(json.dumps([changed_remark]))
        self.assertEqual(parsed[0]["remark"], "Renamed candidate")


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        content_length: int | None = None,
        omit_content_length: bool = False,
        content_encoding: str = "",
    ) -> None:
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding
        self.content_length = (
            None
            if omit_content_length
            else len(body)
            if content_length is None
            else content_length
        )
        self.content = _FakeContent(body)

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        return None


class _FakeSession:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("Missing fake response")
        return self.response


class VpnQualificationGateTest(unittest.IsolatedAsyncioTestCase):
    def _profiles(self):
        return parse_extra_profiles(
            json.dumps(
                [
                    {
                        "remark": "Legacy",
                        "address": "legacy.example.test",
                        "port": 443,
                        "sni": "legacy.example.test",
                        "host": "legacy.example.test",
                        "path": f"/ws-{'1' * 48}",
                    },
                    {
                        "remark": "Qualified candidate",
                        "address": "edge.example.test",
                        "port": 443,
                        "transport": "xhttp",
                        "security": "reality",
                        "path": "/xhttp",
                        "sni": "cover.example.test",
                        "pbk": "A" * 43,
                        "sid": "0011",
                        "fingerprint": "chrome",
                        "qualification_url": QUALIFICATION_URL,
                        "qualification_fingerprint": (
                            QUALIFICATION_FINGERPRINT
                        ),
                        "server_code": "ru-wl-1",
                    },
                ]
            )
        )

    @staticmethod
    def _status(
        *,
        fingerprint: str = QUALIFICATION_FINGERPRINT,
        status: str = "passed",
        valid_until: str = "2026-07-30T12:00:00Z",
    ) -> bytes:
        return json.dumps(
            {
                "service": "ceavpn-whitelist-gate-v1",
                "status": status,
                "config_fingerprint": fingerprint,
                "valid_until": valid_until,
            },
            separators=(",", ":"),
        ).encode()

    async def test_includes_xhttp_only_after_live_qualification_check(
        self,
    ) -> None:
        profiles = self._profiles()
        session = _FakeSession(_FakeResponse(self._status()))
        now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)

        eligible = await qualified_extra_profiles(
            session,  # type: ignore[arg-type]
            profiles,
            now=now,
        )

        self.assertEqual(eligible, profiles)
        self.assertEqual(len(session.calls), 1)
        url, options = session.calls[0]
        self.assertEqual(url, QUALIFICATION_URL)
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["headers"]["Accept"], "application/json")
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(options["timeout"].total, 3)

    async def test_gate_failure_omits_only_xhttp_profile(self) -> None:
        profiles = self._profiles()
        now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
        failures = (
            _FakeSession(error=OSError("unreachable")),
            _FakeSession(_FakeResponse(b"not-json")),
            _FakeSession(_FakeResponse(self._status(), status=302)),
            _FakeSession(
                _FakeResponse(self._status(), content_type="text/plain")
            ),
            _FakeSession(
                _FakeResponse(self._status(), content_encoding="gzip")
            ),
            _FakeSession(
                _FakeResponse(
                    self._status(status="revoked"),
                )
            ),
            _FakeSession(
                _FakeResponse(
                    self._status(fingerprint="b" * 64),
                )
            ),
            _FakeSession(
                _FakeResponse(
                    self._status(valid_until="2026-07-29T12:00:00Z"),
                )
            ),
            _FakeSession(
                _FakeResponse(
                    self._status(valid_until="2026-08-06T12:00:01Z"),
                )
            ),
            _FakeSession(
                _FakeResponse(
                    self._status()[:-1] + b',"unexpected":"field"}',
                )
            ),
            _FakeSession(
                _FakeResponse(
                    b'{"service":"ceavpn-whitelist-gate-v1",'
                    b'"service":"ceavpn-whitelist-gate-v1",'
                    b'"status":"passed",'
                    b'"config_fingerprint":"'
                    + QUALIFICATION_FINGERPRINT.encode()
                    + b'","valid_until":"2026-07-30T12:00:00Z"}'
                )
            ),
            _FakeSession(
                _FakeResponse(
                    b"{" + b" " * 4096 + b"}",
                    content_length=4098,
                )
            ),
            _FakeSession(
                _FakeResponse(
                    b"{" + b" " * 4096 + b"}",
                    omit_content_length=True,
                )
            ),
        )
        for session in failures:
            with self.subTest(session=session):
                eligible = await qualified_extra_profiles(
                    session,  # type: ignore[arg-type]
                    profiles,
                    now=now,
                )
                self.assertEqual(eligible, profiles[:1])


class _FakeDatabase:
    @contextmanager
    def transaction(self):
        yield object()


class VpnQualificationGateRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_rechecks_replica_and_live_gate(
        self,
    ) -> None:
        provider_uuid = "9c97ef67-c753-46d0-9529-74a33f566773"
        upstream_url = "https://upstream.example.test/sub/opaque-token"
        base_profile = (
            f"vless://{provider_uuid}@upstream.example.test:443"
            "?encryption=none&security=tls&type=ws"
            f"&path=%2Fws-{'3' * 48}#Upstream\n"
        ).encode()
        extra_profiles = [
            {
                "remark": "Legacy extra",
                "address": "legacy.example.test",
                "port": 443,
                "sni": "legacy.example.test",
                "host": "legacy.example.test",
                "path": f"/ws-{'1' * 48}",
            },
            {
                "remark": "Whitelist candidate",
                "address": "edge.example.test",
                "port": 443,
                "transport": "xhttp",
                "security": "reality",
                "path": "/xhttp",
                "sni": "cover.example.test",
                "pbk": "A" * 43,
                "sid": "0011",
                "fingerprint": "chrome",
                "qualification_url": QUALIFICATION_URL,
                "qualification_fingerprint": QUALIFICATION_FINGERPRINT,
                "server_code": "ru-wl-1",
            },
        ]
        settings = Settings(
            telegram_bot_token="token",
            database_url="sqlite:///:memory:",
            app_env="test",
            mock_payment_base_url="https://pay.example.test",
            app_base_url="https://bot.example.test",
            vpn_delivery_base_url="https://bot.example.test",
            vpn_delivery_signing_secret="s" * 48,
            vpn_extra_profiles_json=json.dumps(extra_profiles),
        )
        subscription = {
            "id": 42,
            "provider_username": "u_abcdef123456",
            "provider_uuid": provider_uuid,
            "subscription_url": upstream_url,
        }
        token = delivery_subscription_url(subscription, settings).rsplit("/", 1)[
            -1
        ]
        live_until = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        statuses = [
            _FakeResponse(
                VpnQualificationGateTest._status(
                    valid_until=live_until,
                )
            ),
            _FakeResponse(
                VpnQualificationGateTest._status(
                    status="revoked",
                    valid_until=live_until,
                )
            ),
        ]

        class RouteSession:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(
                self,
                exc_type: Any,
                exc: BaseException | None,
                traceback: Any,
            ) -> None:
                return None

            def get(self, url: str, **kwargs: Any) -> _FakeResponse:
                if url == upstream_url:
                    return _FakeResponse(
                        base_profile,
                        content_type="text/plain",
                    )
                if url == QUALIFICATION_URL and statuses:
                    return statuses.pop(0)
                raise AssertionError(f"Unexpected route fetch: {url}")

        app = web.Application()
        register_vpn_subscription_delivery_routes(
            app,
            db=_FakeDatabase(),  # type: ignore[arg-type]
            settings=settings,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        with (
            patch.object(
                VpnSubscriptionRepository,
                "get_by_id",
                return_value=subscription,
            ),
            patch.object(
                VpnSubscriptionRepository,
                "has_completed_server_replica",
                side_effect=(True, False, True),
            ) as replica_ready,
            patch.object(
                VpnSubscriptionDeviceRepository,
                "register_or_touch",
            ),
            patch(
                "ceavpn.vpn_subscription_delivery.ClientSession",
                RouteSession,
            ),
        ):
            qualified = await client.get(f"/sub/{token}")
            qualified_body = await qualified.read()
            pending_replica = await client.get(f"/sub/{token}")
            pending_replica_body = await pending_replica.read()
            revoked = await client.get(f"/sub/{token}")
            revoked_body = await revoked.read()

        self.assertEqual(qualified.status, 200)
        self.assertEqual(pending_replica.status, 200)
        self.assertEqual(revoked.status, 200)
        self.assertEqual(qualified_body.decode().count("vless://"), 3)
        self.assertIn(b"type=xhttp", qualified_body)
        self.assertEqual(pending_replica_body.decode().count("vless://"), 2)
        self.assertNotIn(b"type=xhttp", pending_replica_body)
        self.assertEqual(revoked_body.decode().count("vless://"), 2)
        self.assertNotIn(b"type=xhttp", revoked_body)
        self.assertIn(b"@upstream.example.test:443", revoked_body)
        self.assertIn(b"@legacy.example.test:443", revoked_body)
        self.assertEqual(statuses, [])
        self.assertEqual(replica_ready.call_count, 3)
        for call in replica_ready.call_args_list:
            self.assertEqual(call.kwargs["server_code"], "ru-wl-1")
            self.assertRegex(
                call.kwargs["profile_version"],
                r"^p[0-9a-f]{20}$",
            )


if __name__ == "__main__":
    unittest.main()
