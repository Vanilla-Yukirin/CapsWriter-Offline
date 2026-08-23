"""Environment-backed settings for the CapsWriter Plus HTTP API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit

from capswriter_plus.security import load_required_token, resolve_server_url, validate_token


@dataclass(frozen=True)
class APIConfig:
    token: str
    host: str = "127.0.0.1"
    port: int = 6018
    server_url: str = "ws://127.0.0.1:6016/asr"
    asr_status_url: str = "http://127.0.0.1:6016/status"
    max_upload_bytes: int = 256 * 1024 * 1024
    max_duration_seconds: float = 2 * 60 * 60
    max_concurrency: int = 1
    queue_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 60 * 60
    segment_duration_seconds: float = 60.0
    segment_overlap_seconds: float = 4.0

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "APIConfig":
        source = os.environ if environ is None else environ
        server_url = resolve_server_url(
            source.get("CAPSWRITER_API_ASR_URL")
            or source.get("CAPSWRITER_SERVER_URL")
            or "ws://127.0.0.1:6016/asr",
            addr="127.0.0.1",
            port=6016,
        )
        config = cls(
            token=load_required_token(source),
            host=source.get("CAPSWRITER_API_HOST", "127.0.0.1").strip(),
            port=int(source.get("CAPSWRITER_API_PORT", "6018")),
            server_url=server_url,
            asr_status_url=source.get(
                "CAPSWRITER_API_ASR_STATUS_URL",
                "http://127.0.0.1:6016/status",
            ).strip(),
            max_upload_bytes=int(
                source.get("CAPSWRITER_API_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024))
            ),
            max_duration_seconds=float(
                source.get("CAPSWRITER_API_MAX_DURATION_SECONDS", str(2 * 60 * 60))
            ),
            max_concurrency=int(
                source.get("CAPSWRITER_API_MAX_CONCURRENCY", "1")
            ),
            queue_timeout_seconds=float(
                source.get("CAPSWRITER_API_QUEUE_TIMEOUT_SECONDS", "5")
            ),
            request_timeout_seconds=float(
                source.get("CAPSWRITER_API_REQUEST_TIMEOUT_SECONDS", str(60 * 60))
            ),
            segment_duration_seconds=float(
                source.get("CAPSWRITER_API_SEGMENT_DURATION_SECONDS", "60")
            ),
            segment_overlap_seconds=float(
                source.get("CAPSWRITER_API_SEGMENT_OVERLAP_SECONDS", "4")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        validate_token(self.token)
        resolve_server_url(self.server_url, addr="127.0.0.1", port=6016)
        if not self.host:
            raise ValueError("CAPSWRITER_API_HOST 不能为空")
        if not 1 <= self.port <= 65535:
            raise ValueError("CAPSWRITER_API_PORT 必须在 1..65535 范围内")
        status_url = urlsplit(self.asr_status_url)
        if status_url.scheme not in {"http", "https"} or not status_url.netloc:
            raise ValueError("CAPSWRITER_API_ASR_STATUS_URL 必须是完整的 HTTP(S) URL")
        if status_url.username is not None or status_url.password is not None:
            raise ValueError("CAPSWRITER_API_ASR_STATUS_URL 不能包含凭据")
        if status_url.fragment:
            raise ValueError("CAPSWRITER_API_ASR_STATUS_URL 不能包含 fragment")
        if not 1024 <= self.max_upload_bytes <= 4 * 1024 * 1024 * 1024:
            raise ValueError("CAPSWRITER_API_MAX_UPLOAD_BYTES 必须在 1 KiB 到 4 GiB 之间")
        if not 1 <= self.max_duration_seconds <= 24 * 60 * 60:
            raise ValueError("CAPSWRITER_API_MAX_DURATION_SECONDS 必须在 1 秒到 24 小时之间")
        if not 1 <= self.max_concurrency <= 32:
            raise ValueError("CAPSWRITER_API_MAX_CONCURRENCY 必须在 1..32 范围内")
        if not 0 < self.queue_timeout_seconds <= 300:
            raise ValueError("CAPSWRITER_API_QUEUE_TIMEOUT_SECONDS 必须在 0..300 秒范围内")
        if not 1 <= self.request_timeout_seconds <= 24 * 60 * 60:
            raise ValueError("CAPSWRITER_API_REQUEST_TIMEOUT_SECONDS 必须在 1 秒到 24 小时之间")
        if not 1 <= self.segment_duration_seconds <= 600:
            raise ValueError("CAPSWRITER_API_SEGMENT_DURATION_SECONDS 必须在 1..600 秒范围内")
        if not 0 <= self.segment_overlap_seconds < self.segment_duration_seconds:
            raise ValueError("分段重叠必须非负且小于分段时长")
