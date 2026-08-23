"""Small streaming client for the CapsWriter Plus HTTP transcription API."""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import BinaryIO, Mapping, Optional
from urllib.parse import urlencode, urlsplit

from capswriter_plus.security import load_required_token


class APIClientError(RuntimeError):
    """An error safe to show to an API caller."""


def _endpoint(api_url: str, query: Mapping[str, str]) -> tuple[str, str, int, str]:
    parsed = urlsplit(api_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("CAPSWRITER_API_URL 必须是完整的 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("CAPSWRITER_API_URL 不能包含凭据")
    if parsed.query or parsed.fragment:
        raise ValueError("CAPSWRITER_API_URL 不能包含 query 或 fragment")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/v1/transcriptions?{urlencode(query)}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port, path


def transcribe_file(
    file_path: Path,
    *,
    token: str,
    api_url: str = "http://127.0.0.1:6018",
    response_format: str = "json",
    language: str = "auto",
    context: str = "",
    timeout_seconds: float = 3600,
    chunk_size: int = 1024 * 1024,
) -> tuple[bytes, str]:
    """Stream one local media file and return the response body and media type."""
    path = Path(file_path)
    if not path.is_file():
        raise APIClientError(f"文件不存在或不是普通文件：{path}")
    if response_format not in {"json", "text", "srt"}:
        raise ValueError("response_format 必须是 json、text 或 srt")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    scheme, host, port, request_path = _endpoint(
        api_url,
        {
            "filename": path.name,
            "response_format": response_format,
            "language": language,
            "context": context,
        },
    )
    connection_type = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(host, port, timeout=timeout_seconds)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        connection.putrequest("POST", request_path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", media_type)
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.putheader("Accept", "application/json, text/plain, application/x-subrip")
        connection.endheaders()
        with path.open("rb") as stream:
            _send_stream(connection, stream, chunk_size)
        response = connection.getresponse()
        body = response.read()
        response_type = response.getheader("Content-Type", "application/octet-stream")
        if not 200 <= response.status < 300:
            message = _error_message(body, response_type)
            raise APIClientError(f"HTTP {response.status}: {message}")
        return body, response_type
    except APIClientError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise APIClientError(f"无法连接 CapsWriter HTTP API：{exc}") from exc
    finally:
        connection.close()


def _send_stream(
    connection: http.client.HTTPConnection,
    stream: BinaryIO,
    chunk_size: int,
) -> None:
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        connection.send(chunk)


def _error_message(body: bytes, content_type: str) -> str:
    if "application/json" in content_type.lower():
        try:
            payload = json.loads(body.decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, str) and detail:
                return detail
        except (UnicodeDecodeError, ValueError):
            pass
    text = body[:1000].decode("utf-8", errors="replace").strip()
    return text or "请求失败"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload media to the CapsWriter Plus HTTP transcription API."
    )
    parser.add_argument("file", type=Path, help="local audio or video file")
    parser.add_argument(
        "--api-url",
        default=os.getenv("CAPSWRITER_API_URL", "http://127.0.0.1:6018"),
        help="API base URL (default: CAPSWRITER_API_URL or localhost)",
    )
    parser.add_argument(
        "--format",
        dest="response_format",
        choices=("json", "text", "srt"),
        default="json",
    )
    parser.add_argument("--language", default="auto")
    parser.add_argument("--context", default="")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = load_required_token()
        body, _ = transcribe_file(
            args.file,
            token=token,
            api_url=args.api_url,
            response_format=args.response_format,
            language=args.language,
            context=args.context,
            timeout_seconds=args.timeout,
        )
        if args.output:
            args.output.write_bytes(body)
        else:
            sys.stdout.buffer.write(body)
            if body and not body.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
        return 0
    except (APIClientError, ValueError) as exc:
        print(f"CapsWriter HTTP API 请求失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
