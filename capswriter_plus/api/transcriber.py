"""Network transcription client used by the 6018/TCP API process."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import WebSocketException

from capswriter_plus.security import (
    websocket_client_auth_kwargs,
    websocket_major_version,
    websocket_url_is_local,
)
from core.protocol import AudioMessage, RecognitionMessage


class TranscriptionError(RuntimeError):
    """Base class for failures safe to map to an HTTP error category."""


class MediaDependencyError(TranscriptionError):
    pass


class InvalidMediaError(TranscriptionError):
    pass


class ASRUnavailableError(TranscriptionError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    task_id: str
    text: str
    text_accu: str
    duration_seconds: float
    processing_seconds: float
    tokens: list[str]
    timestamps: list[float]

    @classmethod
    def from_message(
        cls, message: RecognitionMessage, processing_seconds: float
    ) -> "TranscriptionResult":
        return cls(
            task_id=message.task_id,
            text=message.text,
            text_accu=message.text_accu or message.text,
            duration_seconds=float(message.duration),
            processing_seconds=processing_seconds,
            tokens=list(message.tokens),
            timestamps=[float(value) for value in message.timestamps],
        )


async def probe_media_duration(file_path: Path) -> float:
    if shutil.which("ffprobe") is None:
        raise MediaDependencyError("ffprobe is unavailable")
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise InvalidMediaError("ffprobe timed out") from exc
    if process.returncode != 0:
        raise InvalidMediaError("unsupported or invalid media")
    try:
        duration = float(stdout.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidMediaError("media duration is unavailable") from exc
    if duration <= 0:
        raise InvalidMediaError("media duration must be positive")
    return duration


class WebSocketFileTranscriber:
    def __init__(
        self,
        *,
        server_url: str,
        token: str,
        segment_duration_seconds: float,
        segment_overlap_seconds: float,
    ) -> None:
        self.server_url = server_url
        self.token = token
        self.segment_duration_seconds = segment_duration_seconds
        self.segment_overlap_seconds = segment_overlap_seconds

    async def transcribe(
        self,
        file_path: Path,
        *,
        language: str = "auto",
        context: str = "",
    ) -> TranscriptionResult:
        if shutil.which("ffmpeg") is None:
            raise MediaDependencyError("ffmpeg is unavailable")

        started_at = time.monotonic()
        task_id = str(uuid.uuid4())
        kwargs: dict[str, Any] = {
            "uri": self.server_url,
            "subprotocols": ["binary"],
            "max_size": None,
            "max_queue": None,
            "open_timeout": 10,
            "close_timeout": 5,
        }
        kwargs.update(
            websocket_client_auth_kwargs(self.token, websockets.__version__)
        )
        if (
            websocket_major_version(websockets.__version__) >= 15
            and websocket_url_is_local(self.server_url)
        ):
            kwargs["proxy"] = None

        process = None
        try:
            async with websockets.connect(**kwargs) as socket:
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(file_path),
                    "-f",
                    "f32le",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert process.stdout is not None
                chunk_size = int(16000 * 4 * self.segment_duration_seconds)
                while True:
                    data = await process.stdout.read(chunk_size)
                    if not data:
                        break
                    message = AudioMessage(
                        task_id=task_id,
                        source="file",
                        data=base64.b64encode(data).decode("ascii"),
                        is_final=False,
                        time_start=time.time(),
                        seg_duration=self.segment_duration_seconds,
                        seg_overlap=self.segment_overlap_seconds,
                        context=context,
                        language=language,
                    )
                    await socket.send(message.to_json())

                return_code = await process.wait()
                if return_code != 0:
                    raise InvalidMediaError("ffmpeg could not decode the media")

                await socket.send(
                    AudioMessage(
                        task_id=task_id,
                        source="file",
                        data="",
                        is_final=True,
                        time_start=time.time(),
                        seg_duration=self.segment_duration_seconds,
                        seg_overlap=self.segment_overlap_seconds,
                        context=context,
                        language=language,
                    ).to_json()
                )

                while True:
                    raw_message = await socket.recv()
                    payload = json.loads(raw_message)
                    message = RecognitionMessage.from_dict(payload)
                    if message.task_id == task_id and message.is_final:
                        return TranscriptionResult.from_message(
                            message,
                            processing_seconds=time.monotonic() - started_at,
                        )
        except TranscriptionError:
            raise
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise ASRUnavailableError("ASR connection failed") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ASRUnavailableError("ASR returned an invalid response") from exc
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def render_srt(result: TranscriptionResult, max_segment_chars: int = 42) -> str:
    if not result.tokens or not result.timestamps:
        text = (result.text_accu or result.text).strip()
        if not text:
            return ""
        end = max(0.2, result.duration_seconds)
        return f"1\n00:00:00,000 --> {_format_srt_timestamp(end)}\n{text}\n"

    segments: list[tuple[float, float, str]] = []
    buffer: list[str] = []
    segment_start = float(result.timestamps[0])
    strong_punctuation = set("。？！?!")
    pairs = list(zip(result.tokens, result.timestamps))
    for index, (token, timestamp) in enumerate(pairs):
        cleaned = token.replace("@", "")
        buffer.append(cleaned)
        text = "".join(buffer).strip()
        should_split = bool(text) and (
            any(character in strong_punctuation for character in cleaned)
            or len(text) >= max_segment_chars
            or index == len(pairs) - 1
        )
        if not should_split:
            continue
        next_timestamp = (
            float(pairs[index + 1][1])
            if index + 1 < len(pairs)
            else result.duration_seconds
        )
        end = max(float(timestamp) + 0.2, next_timestamp, segment_start + 0.2)
        segments.append((segment_start, end, text))
        buffer = []
        if index + 1 < len(pairs):
            segment_start = float(pairs[index + 1][1])

    blocks = [
        f"{index}\n{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")
