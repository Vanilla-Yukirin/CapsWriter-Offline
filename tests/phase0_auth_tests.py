# coding: utf-8
from __future__ import annotations

import inspect
import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

import websockets

from capswriter_plus.security import (
    AuthFailureGuard,
    MIN_TOKEN_LENGTH,
    SecurityConfigError,
    create_websocket_auth_process_request,
    is_bearer_authorized,
    load_required_token,
    resolve_server_url,
    websocket_client_auth_kwargs,
    websocket_url_is_local,
)


TOKEN = "a" * MIN_TOKEN_LENGTH


async def no_sleep(_delay):
    return None


class FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self.text = text
        self.headers = {}


class FakeConnection:
    remote_address = ("203.0.113.10", 12345)

    def respond(self, status, text):
        return FakeResponse(status, text)


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class SecurityConfigTests(unittest.TestCase):
    def test_missing_token_fails_closed(self):
        with self.assertRaises(SecurityConfigError):
            load_required_token({})

    def test_15_character_token_fails_closed(self):
        with self.assertRaises(SecurityConfigError):
            load_required_token({"CAPSWRITER_TOKEN": "a" * 15})

    def test_16_character_token_loads(self):
        self.assertEqual(
            load_required_token({"CAPSWRITER_TOKEN": "a" * 16}),
            "a" * 16,
        )

    def test_failure_delays_then_returns_rate_limit(self):
        guard = AuthFailureGuard(clock=lambda: 0.0)
        self.assertEqual(guard.register_failure("client"), (0.25, False, None))
        self.assertEqual(guard.register_failure("client"), (0.5, False, None))
        self.assertEqual(guard.register_failure("client"), (1.0, False, None))
        self.assertEqual(guard.register_failure("client"), (2.0, False, None))
        self.assertEqual(guard.register_failure("client"), (0.0, True, 300))

    def test_failure_record_decays_after_five_minutes(self):
        now = [0.0]
        guard = AuthFailureGuard(clock=lambda: now[0])
        guard.register_failure("client")
        now[0] = 300.0
        self.assertEqual(guard.register_failure("client"), (0.25, False, None))

    def test_bearer_header_is_exact_and_case_insensitive(self):
        self.assertTrue(is_bearer_authorized({"authorization": f"bearer {TOKEN}"}, TOKEN))
        self.assertFalse(is_bearer_authorized({"Authorization": f"Bearer {TOKEN}x"}, TOKEN))
        self.assertFalse(is_bearer_authorized({}, TOKEN))

    def test_full_wss_url_preserves_path_and_query(self):
        url = "wss://caps.example.com/asr?client=desktop"
        self.assertEqual(resolve_server_url(url, addr="127.0.0.1", port=6016), url)

    def test_legacy_addr_port_fallback(self):
        self.assertEqual(
            resolve_server_url(None, addr="10.51.192.1", port="6016"),
            "ws://10.51.192.1:6016",
        )

    def test_non_websocket_url_is_rejected(self):
        with self.assertRaises(SecurityConfigError):
            resolve_server_url("https://caps.example.com/asr", addr="x", port=1)

    def test_url_credentials_are_rejected(self):
        with self.assertRaises(SecurityConfigError):
            resolve_server_url(f"wss://user:{TOKEN}@caps.example.com/asr", addr="x", port=1)

    def test_only_local_targets_bypass_system_proxy(self):
        self.assertTrue(websocket_url_is_local("ws://10.51.192.1:6016"))
        self.assertTrue(websocket_url_is_local("ws://127.0.0.1:6016"))
        self.assertFalse(websocket_url_is_local("wss://caps.example.com/asr"))

    def test_client_header_keyword_matches_websockets_generation(self):
        self.assertIn("extra_headers", websocket_client_auth_kwargs(TOKEN, "13.1"))
        self.assertIn("additional_headers", websocket_client_auth_kwargs(TOKEN, "16.0"))


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_websockets_16_accepts_valid_token(self):
        callback = create_websocket_auth_process_request(TOKEN, "16.0", sleep=no_sleep)
        response = callback(FakeConnection(), FakeRequest({"Authorization": f"Bearer {TOKEN}"}))
        if inspect.isawaitable(response):
            response = await response
        self.assertIsNone(response)

    async def test_websockets_16_rejects_missing_token(self):
        callback = create_websocket_auth_process_request(TOKEN, "16.0", sleep=no_sleep)
        response = callback(FakeConnection(), FakeRequest({}))
        if inspect.isawaitable(response):
            response = await response
        self.assertEqual(response.status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    async def test_websockets_16_returns_429_after_four_failures(self):
        callback = create_websocket_auth_process_request(TOKEN, "16.0", sleep=no_sleep)
        connection = FakeConnection()
        for _ in range(4):
            response = await callback(connection, FakeRequest({}))
            self.assertEqual(response.status, HTTPStatus.UNAUTHORIZED)
        response = await callback(connection, FakeRequest({}))
        self.assertEqual(response.status, HTTPStatus.TOO_MANY_REQUESTS)
        self.assertEqual(response.headers["Retry-After"], "300")

    async def test_success_clears_source_failure_record(self):
        callback = create_websocket_auth_process_request(TOKEN, "16.0", sleep=no_sleep)
        connection = FakeConnection()
        for _ in range(4):
            await callback(connection, FakeRequest({}))
        self.assertIsNone(
            await callback(connection, FakeRequest({"Authorization": f"Bearer {TOKEN}"}))
        )
        response = await callback(connection, FakeRequest({}))
        self.assertEqual(response.status, HTTPStatus.UNAUTHORIZED)

    async def test_websockets_13_accepts_valid_token(self):
        callback = create_websocket_auth_process_request(TOKEN, "13.1", sleep=no_sleep)
        response = await callback("/", {"Authorization": f"Bearer {TOKEN}"})
        self.assertIsNone(response)

    async def test_websockets_13_rejects_wrong_token(self):
        callback = create_websocket_auth_process_request(TOKEN, "13.1", sleep=no_sleep)
        status, headers, body = await callback(
            "/asr",
            {"Authorization": f"Bearer {'b' * MIN_TOKEN_LENGTH}"},
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertIn(("WWW-Authenticate", "Bearer"), headers)
        self.assertEqual(body, b"Unauthorized\n")


class SocketManagerConfigTests(unittest.TestCase):
    def test_socket_manager_refuses_to_initialize_without_token(self):
        from core.server.connection.server_manager import SocketManager

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SecurityConfigError):
                SocketManager(object())

    def test_socket_manager_initializes_with_valid_token(self):
        from core.server.connection.server_manager import SocketManager

        with patch.dict(os.environ, {"CAPSWRITER_TOKEN": TOKEN}, clear=True):
            manager = SocketManager(object())
        self.assertEqual(manager._api_token, TOKEN)


class LiveWebSocketHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def handler(websocket):
            await websocket.send("authorized")

        callback = create_websocket_auth_process_request(TOKEN, websockets.__version__)
        self.server = await websockets.serve(
            handler,
            "127.0.0.1",
            0,
            process_request=callback,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_installed_websockets_accepts_bearer_token(self):
        kwargs = websocket_client_auth_kwargs(TOKEN, websockets.__version__)
        async with websockets.connect(f"ws://127.0.0.1:{self.port}", **kwargs) as socket:
            self.assertEqual(await socket.recv(), "authorized")

    async def test_installed_websockets_rejects_missing_token(self):
        with self.assertRaises(Exception):
            async with websockets.connect(f"ws://127.0.0.1:{self.port}"):
                self.fail("未鉴权连接不应建立成功")


if __name__ == "__main__":
    unittest.main()
