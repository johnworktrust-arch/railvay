from __future__ import annotations

import asyncio
import unittest
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from aiohttp.test_utils import TestServer

from ceai.vpn_bot.marzban import (
    MarzbanAuthenticationError,
    MarzbanClient,
    MarzbanNotFoundError,
    MarzbanResponseError,
    MarzbanTimeoutError,
)


@asynccontextmanager
async def running_server(app: web.Application) -> AsyncIterator[str]:
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


class MarzbanClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_http_is_rejected_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            MarzbanClient("http://vpn.example.test", "admin", "secret")
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            MarzbanClient("http://localhost.example.test", "admin", "secret")

    async def test_remote_http_requires_explicit_insecure_opt_in(self) -> None:
        client = MarzbanClient(
            "http://vpn.example.test",
            "admin",
            "secret",
            allow_insecure_http=True,
        )
        self.assertEqual(client.base_url, "http://vpn.example.test")
        await client.close()

    async def test_https_and_loopback_http_are_allowed(self) -> None:
        clients = (
            MarzbanClient("https://vpn.example.test", "admin", "secret"),
            MarzbanClient("http://localhost:8000", "admin", "secret"),
            MarzbanClient("http://127.0.0.1:8000", "admin", "secret"),
            MarzbanClient("http://[::1]:8000", "admin", "secret"),
        )
        for client in clients:
            await client.close()

    async def test_token_is_cached_and_create_user_is_authenticated(self) -> None:
        calls = {"token": 0, "create": 0}

        async def token(request: web.Request) -> web.Response:
            calls["token"] += 1
            form = await request.post()
            self.assertEqual(form["username"], "admin")
            self.assertEqual(form["password"], "secret")
            self.assertEqual(form["grant_type"], "password")
            return web.json_response({"access_token": "token-1"})

        async def create(request: web.Request) -> web.Response:
            calls["create"] += 1
            self.assertEqual(request.headers["Authorization"], "Bearer token-1")
            payload = await request.json()
            return web.json_response(
                {
                    **payload,
                    "subscription_url": "/sub/generated",
                }
            )

        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        app.router.add_post("/api/user", create)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                first_token, second_token = await asyncio.gather(
                    client.get_token(),
                    client.get_token(),
                )
                result = await client.create_user(
                    {"username": "tg_100_d1", "proxies": {"vless": {}}}
                )

        self.assertEqual(first_token, "token-1")
        self.assertEqual(second_token, "token-1")
        self.assertEqual(result["subscription_url"], "/sub/generated")
        self.assertEqual(calls, {"token": 1, "create": 1})

    async def test_401_refreshes_token_once_and_retries_request(self) -> None:
        token_calls = 0
        user_calls = 0

        async def token(request: web.Request) -> web.Response:
            nonlocal token_calls
            token_calls += 1
            return web.json_response({"access_token": f"token-{token_calls}"})

        async def get_user(request: web.Request) -> web.Response:
            nonlocal user_calls
            user_calls += 1
            if request.headers.get("Authorization") == "Bearer token-1":
                return web.json_response({"detail": "Token expired"}, status=401)
            self.assertEqual(request.headers["Authorization"], "Bearer token-2")
            return web.json_response({"username": request.match_info["username"]})

        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        app.router.add_get("/api/user/{username}", get_user)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                result = await client.get_user("tg_100_d1")

        self.assertEqual(result["username"], "tg_100_d1")
        self.assertEqual(token_calls, 2)
        self.assertEqual(user_calls, 2)

    async def test_second_401_is_a_typed_authentication_error(self) -> None:
        async def token(request: web.Request) -> web.Response:
            return web.json_response({"access_token": "always-rejected"})

        async def get_user(request: web.Request) -> web.Response:
            return web.json_response({"detail": "Invalid token"}, status=401)

        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        app.router.add_get("/api/user/{username}", get_user)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                with self.assertRaises(MarzbanAuthenticationError) as raised:
                    await client.get_user("tg_100_d1")

        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(raised.exception.detail, "Invalid token")

    async def test_create_conflict_gets_then_updates_without_rotating_proxy_id(self) -> None:
        operations: list[str] = []
        updated_payload: dict[str, Any] = {}
        existing = {
            "username": "tg_100_d1",
            "proxies": {
                "vless": {
                    "id": "existing-generated-uuid",
                    "flow": "xtls-rprx-vision",
                }
            },
            "expire": 100,
            "status": "active",
        }

        async def token(request: web.Request) -> web.Response:
            return web.json_response({"access_token": "token"})

        async def create(request: web.Request) -> web.Response:
            operations.append("create")
            return web.json_response({"detail": "User already exists"}, status=409)

        async def get_user(request: web.Request) -> web.Response:
            operations.append("get")
            return web.json_response(existing)

        async def update(request: web.Request) -> web.Response:
            operations.append("update")
            updated_payload.update(await request.json())
            return web.json_response({**existing, **updated_payload})

        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        app.router.add_post("/api/user", create)
        app.router.add_get("/api/user/{username}", get_user)
        app.router.add_put("/api/user/{username}", update)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                result = await client.create_user(
                    {
                        "username": "tg_100_d1",
                        "proxies": {"vless": {}},
                        "expire": 200,
                        "status": "active",
                    }
                )

        self.assertEqual(operations, ["create", "get", "update"])
        self.assertNotIn("username", updated_payload)
        self.assertEqual(updated_payload["expire"], 200)
        self.assertEqual(
            updated_payload["proxies"]["vless"],
            {
                "id": "existing-generated-uuid",
                "flow": "xtls-rprx-vision",
            },
        )
        self.assertEqual(result["expire"], 200)

    async def test_not_found_is_typed_and_keeps_status_and_detail(self) -> None:
        async def token(request: web.Request) -> web.Response:
            return web.json_response({"access_token": "token"})

        async def get_user(request: web.Request) -> web.Response:
            return web.json_response({"detail": "User not found"}, status=404)

        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        app.router.add_get("/api/user/{username}", get_user)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                with self.assertRaises(MarzbanNotFoundError) as raised:
                    await client.get_user("missing")

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.detail, "User not found")

    async def test_timeout_raises_typed_timeout_error(self) -> None:
        async def token(request: web.Request) -> web.Response:
            await asyncio.sleep(0.1)
            return web.json_response({"access_token": "late-token"})

        app = web.Application()
        app.router.add_post("/api/admin/token", token)

        async with running_server(app) as base_url:
            async with MarzbanClient(
                base_url,
                "admin",
                "secret",
                timeout=0.01,
            ) as client:
                with self.assertRaises(MarzbanTimeoutError) as raised:
                    await client.get_token()

        self.assertEqual(raised.exception.method, "POST")
        self.assertEqual(raised.exception.path, "/api/admin/token")

    async def test_malformed_token_response_is_typed(self) -> None:
        async def token(request: web.Request) -> web.Response:
            return web.json_response({"token_type": "bearer"})

        app = web.Application()
        app.router.add_post("/api/admin/token", token)

        async with running_server(app) as base_url:
            async with MarzbanClient(base_url, "admin", "secret") as client:
                with self.assertRaises(MarzbanResponseError):
                    await client.get_token()


if __name__ == "__main__":
    unittest.main()
