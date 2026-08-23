"""Shared-token authentication for the HTTP transcription API."""

from __future__ import annotations

import asyncio
import hmac
import logging
from http import HTTPStatus
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException

from capswriter_plus.security import AuthFailureGuard


LOGGER = logging.getLogger("capswriter.api.auth")


class APIAuthenticator:
    def __init__(
        self,
        token: str,
        *,
        failure_guard: Optional[AuthFailureGuard] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.token = token
        self.failure_guard = failure_guard or AuthFailureGuard()
        self.sleep = sleep
        self.lock = asyncio.Lock()

    async def require(self, supplied: str, source: str) -> None:
        authorized = bool(supplied) and hmac.compare_digest(supplied, self.token)
        if authorized:
            async with self.lock:
                self.failure_guard.clear(source)
            return

        async with self.lock:
            delay, limited, retry_after = self.failure_guard.register_failure(source)
        if delay:
            await self.sleep(delay)

        status = HTTPStatus.TOO_MANY_REQUESTS if limited else HTTPStatus.UNAUTHORIZED
        LOGGER.warning("HTTP API 鉴权失败，来源: %s，结果: %s", source, status.value)
        headers = {"Cache-Control": "no-store"}
        if status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        raise HTTPException(
            status_code=status.value,
            detail=status.phrase,
            headers=headers,
        )
