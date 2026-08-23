# coding: utf-8
"""CapsWriter Online 的共享鉴权与连接配置。"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import math
import os
import re
import time
from collections import OrderedDict
from http import HTTPStatus
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit


TOKEN_ENV_NAME = "CAPSWRITER_TOKEN"
SERVER_URL_ENV_NAME = "CAPSWRITER_SERVER_URL"
MIN_TOKEN_LENGTH = 16
AUTH_FAILURE_DELAYS = (0.25, 0.5, 1.0, 2.0)
AUTH_FAILURE_DECAY_SECONDS = 300.0
MAX_TRACKED_AUTH_SOURCES = 4096


class SecurityConfigError(RuntimeError):
    """安全配置缺失或无效。"""


class AuthFailureGuard:
    """记录各来源的连续鉴权失败，并给出延迟或限流决策。"""

    def __init__(
        self,
        *,
        delays: tuple[float, ...] = AUTH_FAILURE_DELAYS,
        decay_seconds: float = AUTH_FAILURE_DECAY_SECONDS,
        max_sources: int = MAX_TRACKED_AUTH_SOURCES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not delays or any(delay < 0 for delay in delays):
            raise ValueError("delays 必须包含非负延迟")
        if decay_seconds <= 0 or max_sources <= 0:
            raise ValueError("decay_seconds 和 max_sources 必须为正数")
        self._delays = delays
        self._decay_seconds = decay_seconds
        self._max_sources = max_sources
        self._clock = clock
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        expired = [
            source
            for source, (_, last_failure) in self._failures.items()
            if now - last_failure >= self._decay_seconds
        ]
        for source in expired:
            self._failures.pop(source, None)

    def register_failure(self, source: str) -> tuple[float, bool, Optional[int]]:
        """返回 (延迟秒数, 是否限流, Retry-After)。"""
        source = source or "unknown"
        now = self._clock()
        self._purge_expired(now)
        count, _ = self._failures.get(source, (0, now))
        count += 1
        self._failures[source] = (count, now)
        self._failures.move_to_end(source)
        while len(self._failures) > self._max_sources:
            self._failures.popitem(last=False)

        if count > len(self._delays):
            return 0.0, True, max(1, math.ceil(self._decay_seconds))
        return self._delays[count - 1], False, None

    def clear(self, source: str) -> None:
        """鉴权成功后清除该来源的失败记录。"""
        self._failures.pop(source or "unknown", None)


def validate_token(token: str, *, env_name: str = TOKEN_ENV_NAME) -> str:
    """校验并返回 token，不允许弱占位值或空白字符。"""
    normalized = token.strip()
    if not normalized:
        raise SecurityConfigError(
            f"缺少必需环境变量 {env_name}；CapsWriter 拒绝启动。"
        )
    if len(normalized) < MIN_TOKEN_LENGTH:
        raise SecurityConfigError(
            f"环境变量 {env_name} 至少需要 {MIN_TOKEN_LENGTH} 个字符；CapsWriter 拒绝启动。"
        )
    if any(char.isspace() for char in normalized):
        raise SecurityConfigError(
            f"环境变量 {env_name} 不能包含空白字符；CapsWriter 拒绝启动。"
        )
    return normalized


def load_required_token(
    environ: Optional[Mapping[str, str]] = None,
    *,
    env_name: str = TOKEN_ENV_NAME,
) -> str:
    """从环境变量读取全局 token；缺失时 fail closed。"""
    source = os.environ if environ is None else environ
    return validate_token(source.get(env_name, ""), env_name=env_name)


def resolve_server_url(
    configured_url: Optional[str],
    *,
    addr: str,
    port: str | int,
) -> str:
    """解析完整 ws/wss URL，同时保留旧 addr/port 配置作为回退。"""
    url = (configured_url or "").strip() or f"ws://{addr}:{port}"
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise SecurityConfigError(
            f"{SERVER_URL_ENV_NAME} 必须使用 ws:// 或 wss://。"
        )
    if not parsed.netloc:
        raise SecurityConfigError(
            f"{SERVER_URL_ENV_NAME} 缺少主机地址。"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SecurityConfigError(
            f"{SERVER_URL_ENV_NAME} 不能在 URL 中嵌入凭据。"
        )
    if parsed.fragment:
        raise SecurityConfigError(
            f"{SERVER_URL_ENV_NAME} 不能包含 URL fragment。"
        )
    return url


def websocket_url_is_local(url: str) -> bool:
    """判断目标是否为应绕过系统代理的本地或私有地址。"""
    host = urlsplit(url).hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def websocket_major_version(version: str) -> int:
    """提取 websockets 主版本号。"""
    match = re.match(r"^(\d+)", version)
    if not match:
        raise SecurityConfigError(f"无法识别 websockets 版本：{version!r}")
    return int(match.group(1))


def websocket_client_auth_kwargs(token: str, websockets_version: str) -> dict[str, Any]:
    """生成不同 websockets 版本使用的客户端鉴权参数。"""
    header = {"Authorization": f"Bearer {validate_token(token)}"}
    if websocket_major_version(websockets_version) >= 14:
        return {"additional_headers": header}
    return {"extra_headers": header}


def _get_header(headers: Any, name: str) -> Optional[str]:
    """兼容 websockets Headers 与普通映射。"""
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is not None:
        return str(value)

    try:
        for key, candidate in headers.items():
            if str(key).lower() == name.lower():
                return str(candidate)
    except AttributeError:
        return None
    return None


def is_bearer_authorized(headers: Any, expected_token: str) -> bool:
    """校验 Authorization: Bearer 请求头。"""
    authorization = _get_header(headers, "Authorization")
    if not authorization:
        return False

    scheme, separator, supplied_token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied_token:
        return False
    return hmac.compare_digest(supplied_token, expected_token)


def _websocket_source(remote_address: Any) -> str:
    """提取不含临时端口的对端地址，作为失败防护键。"""
    if isinstance(remote_address, (tuple, list)) and remote_address:
        return str(remote_address[0])
    return str(remote_address or "unknown")


def create_websocket_auth_process_request(
    expected_token: str,
    websockets_version: str,
    log_warning: Optional[Callable[[str], None]] = None,
    *,
    failure_guard: Optional[AuthFailureGuard] = None,
    sleep: Optional[Callable[[float], Any]] = None,
    status_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
    status_path: str = "/status",
):
    """创建兼容 websockets 13 legacy 与 14+ asyncio API 的鉴权入口。"""
    token = validate_token(expected_token)
    major = websocket_major_version(websockets_version)
    guard = failure_guard or AuthFailureGuard()
    sleep_func = sleep or asyncio.sleep

    def warn(message: str) -> None:
        if log_warning:
            log_warning(message)

    def status_response() -> tuple[HTTPStatus, str]:
        try:
            payload = dict(status_provider()) if status_provider else {}
            return HTTPStatus.OK, json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ) + "\n"
        except Exception:
            warn("ASR 运行状态生成失败，结果: 503")
            return HTTPStatus.SERVICE_UNAVAILABLE, '{"status":"unavailable"}\n'

    def is_status_request(path: object) -> bool:
        return bool(status_provider) and urlsplit(str(path or "/")).path == status_path

    async def rejection_decision(source: str) -> tuple[HTTPStatus, Optional[int]]:
        delay, limited, retry_after = guard.register_failure(source)
        if delay:
            await sleep_func(delay)
        status = HTTPStatus.TOO_MANY_REQUESTS if limited else HTTPStatus.UNAUTHORIZED
        warn(f"WebSocket 握手鉴权失败，来源: {source}，结果: {status.value}")
        return status, retry_after

    if major >= 14:
        async def process_request(connection, request):
            source = _websocket_source(getattr(connection, "remote_address", None))
            if is_bearer_authorized(request.headers, token):
                guard.clear(source)
                if is_status_request(getattr(request, "path", "/")):
                    status, body = status_response()
                    response = connection.respond(status, body)
                    response.headers["Content-Type"] = "application/json; charset=utf-8"
                    response.headers["Cache-Control"] = "no-store"
                    return response
                return None

            status, retry_after = await rejection_decision(source)
            response = connection.respond(status, f"{status.phrase}\n")
            if status == HTTPStatus.UNAUTHORIZED:
                response.headers["WWW-Authenticate"] = "Bearer"
            if retry_after is not None:
                response.headers["Retry-After"] = str(retry_after)
            response.headers["Cache-Control"] = "no-store"
            return response

        return process_request

    async def legacy_process_request(path, request_headers):
        source = "unknown"
        if is_bearer_authorized(request_headers, token):
            guard.clear(source)
            if is_status_request(path):
                status, body = status_response()
                return (
                    status,
                    [
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                    ],
                    body.encode("utf-8"),
                )
            return None

        status, retry_after = await rejection_decision(source)
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ]
        if status == HTTPStatus.UNAUTHORIZED:
            headers.append(("WWW-Authenticate", "Bearer"))
        if retry_after is not None:
            headers.append(("Retry-After", str(retry_after)))
        return (
            status,
            headers,
            f"{status.phrase}\n".encode("ascii"),
        )

    return legacy_process_request
