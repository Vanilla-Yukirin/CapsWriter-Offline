"""CapsWriter Plus 本地只读 Web UI 服务。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from capswriter_plus.security import AuthFailureGuard, load_required_token, validate_token
from capswriter_plus.webui.status import StatusCollector


LOGGER = logging.getLogger("capswriter.webui")
COOKIE_NAME = "capswriter_session"
MAX_LOGIN_BODY_BYTES = 4096
MAX_LOG_BYTES = 128 * 1024
MAX_LOG_LINES = 500


def _env_bool(value: str, *, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法识别布尔值：{value!r}")


@dataclass(frozen=True)
class WebUIConfig:
    token: str
    host: str
    port: int
    base_dir: Path
    log_path: Path
    secure_cookie: bool
    session_ttl_seconds: int
    build_id: str
    instance_name: str = "CapsWriter Plus"
    asr_port: int = 6016
    asr_status_url: str = ""
    api_enabled: bool = False
    api_port: int = 6018
    api_status_url: str = ""

    @classmethod
    def from_env(cls) -> "WebUIConfig":
        base_dir = Path(
            os.getenv("CAPSWRITER_BASE_DIR", str(Path(__file__).resolve().parents[2]))
        ).resolve()
        asr_port = int(os.getenv("CAPSWRITER_ASR_PORT", "6016"))
        api_port = int(os.getenv("CAPSWRITER_API_PORT", "6018"))
        config = cls(
            token=load_required_token(),
            host=os.getenv("CAPSWRITER_WEBUI_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("CAPSWRITER_WEBUI_PORT", "6017")),
            base_dir=base_dir,
            log_path=Path(
                os.getenv("CAPSWRITER_LOG_PATH", str(base_dir / "logs" / "server_latest.log"))
            ).resolve(),
            secure_cookie=_env_bool(
                os.getenv("CAPSWRITER_WEBUI_SECURE_COOKIE", "0")
            ),
            session_ttl_seconds=int(
                os.getenv("CAPSWRITER_WEBUI_SESSION_TTL", "43200")
            ),
            build_id=os.getenv("CAPSWRITER_BUILD_ID", "development").strip()
            or "development",
            instance_name=os.getenv(
                "CAPSWRITER_INSTANCE_NAME", "CapsWriter Plus"
            ).strip(),
            asr_port=asr_port,
            asr_status_url=os.getenv(
                "CAPSWRITER_ASR_STATUS_URL",
                f"http://127.0.0.1:{asr_port}/status",
            ).strip(),
            api_enabled=_env_bool(os.getenv("CAPSWRITER_API_ENABLED", "0")),
            api_port=api_port,
            api_status_url=os.getenv(
                "CAPSWRITER_API_STATUS_URL",
                f"http://127.0.0.1:{api_port}/readyz",
            ).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        import ipaddress

        validate_token(self.token)
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("Web UI 必须使用回环 IP 地址") from exc
        if not address.is_loopback:
            raise ValueError("Web UI 只允许监听回环地址")
        if not 1 <= self.port <= 65535:
            raise ValueError("CAPSWRITER_WEBUI_PORT 必须在 1..65535 范围内")
        if not 300 <= self.session_ttl_seconds <= 86400:
            raise ValueError("Web UI 会话有效期必须在 5 分钟到 24 小时之间")
        if not self.instance_name or len(self.instance_name) > 80:
            raise ValueError("CAPSWRITER_INSTANCE_NAME 必须为 1..80 个字符")
        if any(ord(char) < 32 for char in self.instance_name):
            raise ValueError("CAPSWRITER_INSTANCE_NAME 不能包含控制字符")
        if not 1 <= self.asr_port <= 65535:
            raise ValueError("CAPSWRITER_ASR_PORT 必须在 1..65535 范围内")
        status_url = urlsplit(
            self.asr_status_url
            or f"http://127.0.0.1:{self.asr_port}/status"
        )
        if status_url.scheme not in {"http", "https"} or not status_url.netloc:
            raise ValueError("CAPSWRITER_ASR_STATUS_URL 必须是完整的 HTTP(S) URL")
        if status_url.username is not None or status_url.password is not None:
            raise ValueError("CAPSWRITER_ASR_STATUS_URL 不能包含凭据")
        if status_url.fragment:
            raise ValueError("CAPSWRITER_ASR_STATUS_URL 不能包含 fragment")
        if not 1 <= self.api_port <= 65535:
            raise ValueError("CAPSWRITER_API_PORT 必须在 1..65535 范围内")
        if self.api_enabled:
            api_status_url = urlsplit(
                self.api_status_url
                or f"http://127.0.0.1:{self.api_port}/readyz"
            )
            if (
                api_status_url.scheme not in {"http", "https"}
                or not api_status_url.netloc
            ):
                raise ValueError("CAPSWRITER_API_STATUS_URL 必须是完整的 HTTP(S) URL")
            if (
                api_status_url.username is not None
                or api_status_url.password is not None
            ):
                raise ValueError("CAPSWRITER_API_STATUS_URL 不能包含凭据")
            if api_status_url.fragment:
                raise ValueError("CAPSWRITER_API_STATUS_URL 不能包含 fragment")


class SessionStore:
    """保存会话令牌摘要；重启 Web UI 会使全部会话失效。"""

    def __init__(self, ttl_seconds: int, max_sessions: int = 32) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purge(self, now: float) -> None:
        expired = [digest for digest, expires in self._sessions.items() if expires <= now]
        for digest in expired:
            self._sessions.pop(digest, None)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions, key=self._sessions.get)
                self._sessions.pop(oldest, None)
            self._sessions[self._digest(token)] = now + self.ttl_seconds
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.monotonic()
        digest = self._digest(token)
        with self._lock:
            self._purge(now)
            expires = self._sessions.get(digest)
            return expires is not None and expires > now

    def invalidate(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(self._digest(token), None)


def read_log_chunk(
    log_path: Path,
    cursor: Optional[int],
    *,
    max_bytes: int = MAX_LOG_BYTES,
    max_lines: int = MAX_LOG_LINES,
) -> dict[str, Any]:
    """读取固定日志文件；cursor 缺失时返回尾部，truncate 后自动复位。"""
    try:
        size = log_path.stat().st_size
    except OSError:
        return {"available": False, "lines": [], "cursor": 0, "reset": False}

    reset = cursor is not None and (cursor < 0 or cursor > size)
    try:
        with log_path.open("rb") as stream:
            if cursor is None:
                start = max(0, size - max_bytes)
                stream.seek(start)
                data = stream.read(max_bytes)
                if start > 0:
                    _, separator, data = data.partition(b"\n")
                    if not separator:
                        data = b""
                lines = data.decode("utf-8", errors="replace").splitlines()[-max_lines:]
                next_cursor = size
            else:
                start = 0 if reset else cursor
                stream.seek(start)
                data = stream.read(max_bytes)
                next_cursor = stream.tell()
                lines = data.decode("utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return {"available": False, "lines": [], "cursor": 0, "reset": False}

    return {
        "available": True,
        "lines": lines,
        "cursor": next_cursor,
        "reset": reset,
    }


class WebUIRuntime:
    def __init__(
        self,
        config: WebUIConfig,
        *,
        failure_guard: Optional[AuthFailureGuard] = None,
        status_collector: Optional[StatusCollector] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.started_at = time.monotonic()
        self.sessions = SessionStore(config.session_ttl_seconds)
        self.failure_guard = failure_guard or AuthFailureGuard()
        self.failure_lock = threading.Lock()
        self.sleep = sleep
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.status_collector = status_collector or StatusCollector(
            token=config.token,
            instance_name=config.instance_name,
            asr_status_url=(
                config.asr_status_url
                or f"http://127.0.0.1:{config.asr_port}/status"
            ),
            asr_port=config.asr_port,
            webui_host=config.host,
            webui_port=config.port,
            log_path=config.log_path,
            build_id=config.build_id,
            webui_started_at=self.started_at,
            api_enabled=config.api_enabled,
            api_status_url=(
                config.api_status_url
                or f"http://127.0.0.1:{config.api_port}/readyz"
            ),
            api_port=config.api_port,
        )

    def authenticate_token(self, supplied: str, source: str) -> bool:
        authorized = bool(supplied) and hmac.compare_digest(supplied, self.config.token)
        if authorized:
            with self.failure_lock:
                self.failure_guard.clear(source)
        return authorized

    def register_failure(self, source: str) -> tuple[HTTPStatus, Optional[int]]:
        with self.failure_lock:
            delay, limited, retry_after = self.failure_guard.register_failure(source)
        if delay:
            self.sleep(delay)
        status = HTTPStatus.TOO_MANY_REQUESTS if limited else HTTPStatus.UNAUTHORIZED
        LOGGER.warning("Web UI 登录鉴权失败，来源: %s，结果: %s", source, status.value)
        return status, retry_after


class CapsWriterHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, runtime: WebUIRuntime):
        self.runtime = runtime
        super().__init__(address, handler)


class WebUIRequestHandler(BaseHTTPRequestHandler):
    server_version = "CapsWriterWebUI/1.0"
    sys_version = ""

    @property
    def runtime(self) -> WebUIRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def source(self) -> str:
        return str(self.client_address[0]) if self.client_address else "unknown"

    def log_message(self, message: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.source, message % args)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes = b"",
        *,
        content_type: str = "application/octet-stream",
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            status,
            body,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    def _cookie_value(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return ""
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _bearer_token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return ""
        return token

    def _require_auth(self) -> bool:
        bearer = self._bearer_token()
        if bearer:
            if self.runtime.authenticate_token(bearer, self.source):
                return True
            status, retry_after = self.runtime.register_failure(self.source)
            headers = {"WWW-Authenticate": "Bearer"}
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            self._send_json(status, {"error": "unauthorized"}, headers=headers)
            return False
        if self.runtime.sessions.valid(self._cookie_value()):
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _serve_static(self, relative_path: str, content_type: str) -> None:
        path = self.runtime.static_dir / relative_path
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_bytes(HTTPStatus.OK, body, content_type=content_type)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif path == "/assets/app.css":
            self._serve_static("app.css", "text/css; charset=utf-8")
        elif path == "/assets/app.js":
            self._serve_static("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/session":
            if self._require_auth():
                self._send_json(HTTPStatus.OK, {"authenticated": True})
        elif path == "/api/overview":
            if self._require_auth():
                self._send_json(HTTPStatus.OK, self.runtime.status_collector.overview())
        elif path == "/api/config":
            if self._require_auth():
                self._send_json(HTTPStatus.OK, self.runtime.status_collector.safe_config())
        elif path == "/api/logs":
            if not self._require_auth():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            cursor_text = query.get("cursor", [None])[0]
            try:
                cursor = None if cursor_text in {None, ""} else int(cursor_text)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
                return
            self._send_json(
                HTTPStatus.OK,
                read_log_chunk(self.runtime.config.log_path, cursor),
            )
        elif path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return None
        if length <= 0 or length > MAX_LOGIN_BODY_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > MAX_LOGIN_BODY_BYTES else HTTPStatus.BAD_REQUEST
            self._send_json(status, {"error": "invalid_request"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return None
        return payload

    def _session_cookie(self, value: str, max_age: int) -> str:
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = value
        morsel = cookie[COOKIE_NAME]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["max-age"] = str(max_age)
        if self.runtime.config.secure_cookie:
            morsel["secure"] = True
        return morsel.OutputString()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/login":
            payload = self._read_json_body()
            if payload is None:
                return
            supplied = payload.get("token")
            if isinstance(supplied, str) and self.runtime.authenticate_token(supplied, self.source):
                session = self.runtime.sessions.create()
                self._send_json(
                    HTTPStatus.OK,
                    {"authenticated": True},
                    headers={
                        "Set-Cookie": self._session_cookie(
                            session, self.runtime.config.session_ttl_seconds
                        )
                    },
                )
                return
            status, retry_after = self.runtime.register_failure(self.source)
            headers = {"WWW-Authenticate": "Bearer"}
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            self._send_json(status, {"error": "unauthorized"}, headers=headers)
        elif path == "/api/logout":
            self.runtime.sessions.invalidate(self._cookie_value())
            self._send_json(
                HTTPStatus.OK,
                {"authenticated": False},
                headers={"Set-Cookie": self._session_cookie("", 0)},
            )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})


def create_server(
    config: WebUIConfig,
    *,
    bind_port: Optional[int] = None,
    failure_guard: Optional[AuthFailureGuard] = None,
    status_collector: Optional[StatusCollector] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CapsWriterHTTPServer:
    config.validate()
    runtime = WebUIRuntime(
        config,
        failure_guard=failure_guard,
        status_collector=status_collector,
        sleep=sleep,
    )
    return CapsWriterHTTPServer(
        (config.host, config.port if bind_port is None else bind_port),
        WebUIRequestHandler,
        runtime,
    )


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = WebUIConfig.from_env()
    server = create_server(config)
    LOGGER.info("CapsWriter Web UI 已启动：http://%s:%s", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOGGER.info("CapsWriter Web UI 已停止")
