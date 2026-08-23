# coding: utf-8
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from capswriter_plus.security import AuthFailureGuard, SecurityConfigError
from capswriter_plus.webui.server import WebUIConfig, create_server, read_log_chunk
from capswriter_plus.webui.status import StatusCollector


TOKEN = "phase1a-test-token-123456"


class StubStatusCollector:
    def overview(self):
        return {
            "generated_at": "2026-08-23T09:00:00+00:00",
            "instance": {"name": "Test Instance"},
            "version": {"capswriter": "2.6", "extension": "CapsWriter Plus Web UI", "build": "test"},
            "asr": {
                "active": True,
                "ready": True,
                "state": "active",
                "main_pid": 123,
                "worker_pid": 124,
                "uptime_seconds": 60,
                "port": 6016,
                "listening": True,
                "connections": 1,
            },
            "webui": {"active": True, "state": "active", "pid": 125, "port": 6017, "listen": "127.0.0.1", "uptime_seconds": 10},
            "device": {"mode": "gpu", "label": "GGUF decoder: GPU", "components": []},
            "log": {"available": True, "size_bytes": 10, "modified_at": None, "filename": "server_latest.log"},
        }

    def safe_config(self):
        return {
            "server": {"listen": "0.0.0.0", "port": 6016, "model_type": "qwen_asr"},
            "security": {"token_configured": True, "minimum_token_length": 16, "token_visible": False},
        }


class WebUITestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "server_latest.log"
        self.log_path.write_text("10:00:00 INFO  server ready\n10:00:01 WARNING sample warning\n", encoding="utf-8")
        self.config = WebUIConfig(
            token=TOKEN,
            host="127.0.0.1",
            port=6017,
            base_dir=Path(self.temp_dir.name),
            log_path=self.log_path,
            secure_cookie=False,
            session_ttl_seconds=3600,
            build_id="test",
            instance_name="Test Instance",
        )
        self.server = create_server(
            self.config,
            bind_port=0,
            failure_guard=AuthFailureGuard(delays=(0, 0, 0, 0)),
            status_collector=StubStatusCollector(),
            sleep=lambda _: None,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, method, path, *, payload=None, headers=None):
        body = None
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def login(self):
        status, headers, body = self.request("POST", "/api/login", payload={"token": TOKEN})
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_health_is_minimal_and_unauthenticated(self):
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_dashboard_and_assets_are_served_without_inline_code(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("CapsWriter Plus", html)
        self.assertIn('src="/assets/app.js"', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("YUKIRIN", html)
        self.assertNotIn("LXC", html)
        self.assertEqual(self.request("GET", "/assets/app.css")[0], 200)
        self.assertEqual(self.request("GET", "/assets/app.js")[0], 200)

    def test_private_apis_reject_anonymous_requests(self):
        for path in ("/api/session", "/api/overview", "/api/config", "/api/logs"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 401, (path, body))
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_valid_login_sets_hardened_session_cookie(self):
        status, headers, body = self.request("POST", "/api/login", payload={"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True})
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn(TOKEN, cookie)
        self.assertNotIn("Secure", cookie)

    def test_session_cookie_unlocks_overview_and_config_without_token_leak(self):
        cookie = self.login()
        for path in ("/api/session", "/api/overview", "/api/config"):
            status, _, body = self.request("GET", path, headers={"Cookie": cookie})
            self.assertEqual(status, 200, (path, body))
            self.assertNotIn(TOKEN.encode(), body)

    def test_bearer_token_unlocks_read_only_api(self):
        status, _, body = self.request(
            "GET", "/api/config", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)

    def test_failed_bearer_auth_is_rate_limited(self):
        headers = {"Authorization": f"Bearer {'x' * 16}"}
        for _ in range(4):
            status, _, _ = self.request("GET", "/api/config", headers=headers)
            self.assertEqual(status, 401)
        status, response_headers, body = self.request(
            "GET", "/api/config", headers=headers
        )
        self.assertEqual(status, 429, body)
        self.assertEqual(response_headers["Retry-After"], "300")

    def test_successful_bearer_auth_clears_failure_record(self):
        wrong_headers = {"Authorization": f"Bearer {'x' * 16}"}
        for _ in range(4):
            self.request("GET", "/api/config", headers=wrong_headers)
        self.assertEqual(
            self.request(
                "GET", "/api/config", headers={"Authorization": f"Bearer {TOKEN}"}
            )[0],
            200,
        )
        self.assertEqual(
            self.request("GET", "/api/config", headers=wrong_headers)[0], 401
        )

    def test_failed_login_returns_429_after_four_failures(self):
        for _ in range(4):
            status, _, _ = self.request("POST", "/api/login", payload={"token": "x" * 16})
            self.assertEqual(status, 401)
        status, headers, body = self.request("POST", "/api/login", payload={"token": "x" * 16})
        self.assertEqual(status, 429, body)
        self.assertEqual(headers["Retry-After"], "300")

    def test_successful_login_clears_failure_record(self):
        for _ in range(4):
            self.request("POST", "/api/login", payload={"token": "x" * 16})
        self.login()
        status, _, _ = self.request("POST", "/api/login", payload={"token": "x" * 16})
        self.assertEqual(status, 401)

    def test_logout_invalidates_session(self):
        cookie = self.login()
        status, headers, _ = self.request("POST", "/api/logout", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/session", headers={"Cookie": cookie})[0], 401)

    def test_logs_use_fixed_file_and_support_cursor(self):
        cookie = self.login()
        status, _, body = self.request("GET", "/api/logs?path=/etc/passwd", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertEqual(len(first["lines"]), 2)
        self.assertNotIn("root:", body.decode("utf-8"))
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write("10:00:02 ERROR sample error\n")
        status, _, body = self.request("GET", f'/api/logs?cursor={first["cursor"]}', headers={"Cookie": cookie})
        second = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(second["lines"], ["10:00:02 ERROR sample error"])

    def test_log_truncate_resets_cursor(self):
        first = read_log_chunk(self.log_path, None)
        self.log_path.write_text("new log\n", encoding="utf-8")
        second = read_log_chunk(self.log_path, first["cursor"])
        self.assertTrue(second["reset"])
        self.assertEqual(second["lines"], ["new log"])

    def test_invalid_json_and_large_body_are_rejected(self):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        connection.request("POST", "/api/login", body=b"{", headers={"Content-Length": "1"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        connection.close()

        status, _, _ = self.request(
            "POST", "/api/login", headers={"Content-Length": "5000"}
        )
        self.assertEqual(status, 413)


class WebUIConfigTests(unittest.TestCase):
    def make_config(self, *, token=TOKEN, host="127.0.0.1"):
        return WebUIConfig(
            token=token,
            host=host,
            port=6017,
            base_dir=Path.cwd(),
            log_path=Path.cwd() / "logs" / "server_latest.log",
            secure_cookie=False,
            session_ttl_seconds=3600,
            build_id="test",
            instance_name="Test Instance",
        )

    def test_non_loopback_listener_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_config(host="0.0.0.0").validate()

    def test_short_token_is_rejected(self):
        with self.assertRaises(SecurityConfigError):
            self.make_config(token="a" * 15).validate()

    def test_instance_name_is_bounded(self):
        with self.assertRaises(ValueError):
            replace(self.make_config(), instance_name="").validate()


class GenericStatusCollectorTests(unittest.TestCase):
    def test_overview_has_no_deployment_specific_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = StatusCollector(
                instance_name="Generic Instance",
                asr_port=6016,
                webui_host="127.0.0.1",
                webui_port=6017,
                log_path=Path(temp_dir) / "missing.log",
                build_id="test",
                webui_started_at=0,
            )
            overview = collector.overview()

        self.assertEqual(overview["instance"]["name"], "Generic Instance")
        self.assertIsNone(overview["asr"]["ready"])
        self.assertEqual(overview["device"]["mode"], "unknown")
        self.assertNotIn("wrapper", overview)
        self.assertNotIn("gpu", overview)


if __name__ == "__main__":
    unittest.main()
