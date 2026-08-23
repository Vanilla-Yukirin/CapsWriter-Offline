"""FastAPI application for the CapsWriter Plus 6018/TCP API."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from capswriter_plus.api.auth import APIAuthenticator
from capswriter_plus.api.settings import APIConfig
from capswriter_plus.api.transcriber import (
    ASRUnavailableError,
    InvalidMediaError,
    MediaDependencyError,
    TranscriptionResult,
    WebSocketFileTranscriber,
    probe_media_duration,
    render_srt,
)
from capswriter_plus.security import AuthFailureGuard


LOGGER = logging.getLogger("capswriter.api")
BEARER_SCHEME = HTTPBearer(auto_error=False)


class ResponseFormat(str, Enum):
    json = "json"
    text = "text"
    srt = "srt"


class TranscriptionResponse(BaseModel):
    id: str
    status: str = "completed"
    text: str
    duration_seconds: float = Field(ge=0)
    processing_seconds: float = Field(ge=0)
    language: str
    tokens: list[str]
    timestamps: list[float]
    srt: str


def _source(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def require_api_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(BEARER_SCHEME),
) -> None:
    supplied = ""
    if credentials and credentials.scheme.lower() == "bearer":
        supplied = credentials.credentials
    await request.app.state.runtime.authenticator.require(supplied, _source(request))


def _safe_filename(filename: str) -> str:
    leaf = re.split(r"[/\\]", filename.strip())[-1]
    cleaned = re.sub(r"[^\w. -]", "_", leaf, flags=re.UNICODE).strip(" .")
    return (cleaned or "upload.bin")[:180]


async def _write_request_body(
    request: Request, destination: Path, max_bytes: int
) -> int:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        raise HTTPException(
            status_code=415,
            detail="Use the media bytes as the request body, not multipart/form-data.",
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Upload is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.") from exc

    total = 0
    with destination.open("wb") as stream:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail="Upload is too large.")
            stream.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Request body is empty.")
    return total


def _fetch_asr_status(config: APIConfig) -> dict[str, Any] | None:
    request = urllib.request.Request(
        config.asr_status_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            raw = response.read(128 * 1024 + 1)
    except (OSError, urllib.error.URLError):
        return None
    if len(raw) > 128 * 1024:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def default_readiness_probe(config: APIConfig) -> dict[str, Any]:
    asr_status = await asyncio.to_thread(_fetch_asr_status, config)
    asr_payload = asr_status.get("asr") if asr_status else None
    asr_ready = bool(
        isinstance(asr_payload, dict) and asr_payload.get("ready") is True
    )
    dependencies = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
    return {
        "ready": asr_ready and all(dependencies.values()),
        "asr": "ready" if asr_ready else "unavailable",
        "dependencies": dependencies,
    }


class APIRuntime:
    def __init__(
        self,
        config: APIConfig,
        *,
        transcriber: Any,
        readiness_probe: Callable[[], Awaitable[dict[str, Any]]],
        failure_guard: Optional[AuthFailureGuard] = None,
        auth_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.transcriber = transcriber
        self.readiness_probe = readiness_probe
        self.authenticator = APIAuthenticator(
            config.token,
            failure_guard=failure_guard,
            sleep=auth_sleep,
        )
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self.active_requests = 0

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(
                self.semaphore.acquire(),
                timeout=self.config.queue_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="Transcription capacity is busy.",
                headers={
                    "Retry-After": str(
                        max(1, math.ceil(self.config.queue_timeout_seconds))
                    )
                },
            ) from exc
        self.active_requests += 1

    def release(self) -> None:
        self.active_requests = max(0, self.active_requests - 1)
        self.semaphore.release()


def _json_result(
    result: TranscriptionResult, language: str
) -> TranscriptionResponse:
    return TranscriptionResponse(
        id=result.task_id,
        text=result.text_accu or result.text,
        duration_seconds=result.duration_seconds,
        processing_seconds=result.processing_seconds,
        language=language,
        tokens=result.tokens,
        timestamps=result.timestamps,
        srt=render_srt(result),
    )


def create_app(
    config: APIConfig,
    *,
    transcriber: Any = None,
    readiness_probe: Optional[
        Callable[[], Awaitable[dict[str, Any]]]
    ] = None,
    failure_guard: Optional[AuthFailureGuard] = None,
    auth_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    config.validate()
    actual_transcriber = transcriber or WebSocketFileTranscriber(
        server_url=config.server_url,
        token=config.token,
        segment_duration_seconds=config.segment_duration_seconds,
        segment_overlap_seconds=config.segment_overlap_seconds,
    )
    actual_readiness_probe = readiness_probe or (
        lambda: default_readiness_probe(config)
    )
    runtime = APIRuntime(
        config,
        transcriber=actual_transcriber,
        readiness_probe=actual_readiness_probe,
        failure_guard=failure_guard,
        auth_sleep=auth_sleep,
    )
    app = FastAPI(
        title="CapsWriter Plus HTTP Transcription API",
        version="1.0.0",
        description=(
            "Upload media bytes and receive transcription results from the existing "
            "CapsWriter WebSocket ASR service. Send Authorization: Bearer <token>."
        ),
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def no_store_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": "CapsWriter Plus HTTP Transcription API",
            "version": "v1",
            "docs": "/docs",
            "health": "/healthz",
        }

    @app.get("/healthz", tags=["health"])
    async def healthz():
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["health"],
        dependencies=[Depends(require_api_token)],
    )
    async def readyz():
        status = await runtime.readiness_probe()
        return JSONResponse(
            status_code=200 if status.get("ready") is True else 503,
            content=status,
        )

    @app.post(
        "/v1/transcriptions",
        tags=["transcriptions"],
        response_model=TranscriptionResponse,
        dependencies=[Depends(require_api_token)],
        responses={
            200: {
                "description": "Completed transcription in the requested format.",
                "content": {
                    "text/plain": {},
                    "application/x-subrip": {},
                },
            },
            400: {"description": "Empty or malformed request."},
            413: {"description": "Upload or media duration exceeds configured limits."},
            415: {"description": "Multipart is not accepted; send raw media bytes."},
            422: {"description": "Unsupported or invalid media."},
            429: {"description": "Authentication or transcription capacity limit."},
            503: {"description": "ASR or media dependency is unavailable."},
            504: {"description": "Transcription timed out."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "audio/wav": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                },
            }
        },
    )
    async def transcriptions(
        request: Request,
        filename: str = Query("upload.bin", min_length=1, max_length=255),
        response_format: ResponseFormat = Query(ResponseFormat.json),
        language: str = Query("auto", min_length=2, max_length=32),
        context: str = Query("", max_length=2000),
    ):
        await runtime.acquire()
        try:
            with tempfile.TemporaryDirectory(prefix="capswriter-plus-api-") as temp_dir:
                file_path = Path(temp_dir) / _safe_filename(filename)
                await _write_request_body(
                    request, file_path, config.max_upload_bytes
                )

                async def run_transcription():
                    duration = await probe_media_duration(file_path)
                    if duration > config.max_duration_seconds:
                        raise HTTPException(
                            status_code=413,
                            detail="Media duration exceeds the configured limit.",
                        )
                    return await runtime.transcriber.transcribe(
                        file_path,
                        language=language,
                        context=context,
                    )

                try:
                    result = await asyncio.wait_for(
                        run_transcription(),
                        timeout=config.request_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise HTTPException(
                        status_code=504, detail="Transcription timed out."
                    ) from exc
                except InvalidMediaError as exc:
                    raise HTTPException(
                        status_code=422, detail="Unsupported or invalid media."
                    ) from exc
                except MediaDependencyError as exc:
                    raise HTTPException(
                        status_code=503, detail="Media processing is unavailable."
                    ) from exc
                except ASRUnavailableError as exc:
                    raise HTTPException(
                        status_code=503, detail="ASR service is unavailable."
                    ) from exc

                payload = _json_result(result, language)
                if response_format == ResponseFormat.text:
                    return PlainTextResponse(payload.text + "\n")
                if response_format == ResponseFormat.srt:
                    return Response(
                        payload.srt,
                        media_type="application/x-subrip",
                        headers={
                            "Content-Disposition": 'attachment; filename="transcription.srt"'
                        },
                    )
                return payload
        finally:
            runtime.release()

    return app


def run() -> None:
    import uvicorn

    config = APIConfig.from_env()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )
