# coding: utf-8
"""CapsWriter Online 的共享鉴权与连接配置。"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from http import HTTPStatus
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit


TOKEN_ENV_NAME = "CAPSWRITER_TOKEN"
SERVER_URL_ENV_NAME = "CAPSWRITER_SERVER_URL"
MIN_TOKEN_LENGTH = 32


class SecurityConfigError(RuntimeError):
    """安全配置缺失或无效。"""


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


def create_websocket_auth_process_request(
    expected_token: str,
    websockets_version: str,
    log_warning: Optional[Callable[[str], None]] = None,
):
    """创建兼容 websockets 13 legacy 与 14+ asyncio API 的握手鉴权器。"""
    token = validate_token(expected_token)
    major = websocket_major_version(websockets_version)

    def warn(message: str) -> None:
        if log_warning:
            log_warning(message)

    if major >= 14:
        def process_request(connection, request):
            if is_bearer_authorized(request.headers, token):
                return None

            remote = getattr(connection, "remote_address", None)
            warn(f"拒绝未授权 WebSocket 握手，来源: {remote or 'unknown'}")
            response = connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
            response.headers["WWW-Authenticate"] = "Bearer"
            response.headers["Cache-Control"] = "no-store"
            return response

        return process_request

    async def legacy_process_request(path, request_headers):
        if is_bearer_authorized(request_headers, token):
            return None

        warn(f"拒绝未授权 WebSocket 握手，路径: {path}")
        return (
            HTTPStatus.UNAUTHORIZED,
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("WWW-Authenticate", "Bearer"),
                ("Cache-Control", "no-store"),
            ],
            b"Unauthorized\n",
        )

    return legacy_process_request
