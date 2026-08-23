# coding: utf-8
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from capswriter_plus.api.server import APIRuntime, create_app
from capswriter_plus.api.settings import APIConfig
from capswriter_plus.api.transcriber import TranscriptionResult, render_srt
from capswriter_plus.security import AuthFailureGuard, SecurityConfigError


TOKEN = "phase1b-api-token-123456"


async def no_sleep(_delay):
    return None


class StubTranscriber:
    def __init__(self):
        self.calls = []

    async def transcribe(self, file_path: Path, *, language: str, context: str):
        self.calls.append(
            {
                "filename": file_path.name,
                "body": file_path.read_bytes(),
                "language": language,
                "context": context,
            }
        )
        return TranscriptionResult(
            task_id="task-123",
            text="你好。",
            text_accu="你好。",
            duration_seconds=1.25,
            processing_seconds=0.5,
            tokens=["你", "好", "。"],
            timestamps=[0.0, 0.4, 0.8],
        )


class APITestCase(unittest.TestCase):
    def setUp(self):
        self.config = APIConfig(
            token=TOKEN,
            max_upload_bytes=1024,
            max_duration_seconds=60,
            queue_timeout_seconds=0.1,
            request_timeout_seconds=5,
        )
        self.transcriber = StubTranscriber()

        async def ready():
            return {
                "ready": True,
                "asr": "ready",
                "dependencies": {"ffmpeg": True, "ffprobe": True},
            }

        self.app = create_app(
            self.config,
            transcriber=self.transcriber,
            readiness_probe=ready,
            failure_guard=AuthFailureGuard(delays=(0, 0, 0, 0)),
            auth_sleep=no_sleep,
        )
        self.client = TestClient(self.app)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self):
        self.client.close()

    def post(self, *, suffix="", body=b"RIFFfake", headers=None):
        request_headers = {**self.auth, "Content-Type": "audio/wav"}
        request_headers.update(headers or {})
        with patch(
            "capswriter_plus.api.server.probe_media_duration",
            new=AsyncMock(return_value=1.25),
        ):
            return self.client.post(
                f"/v1/transcriptions?filename=sample.wav{suffix}",
                content=body,
                headers=request_headers,
            )

    def test_health_is_minimal_and_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_requires_the_shared_token(self):
        self.assertEqual(self.client.get("/readyz").status_code, 401)
        response = self.client.get("/readyz", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])

    def test_raw_upload_returns_structured_json(self):
        response = self.post(suffix="&language=chinese&context=greeting")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["id"], "task-123")
        self.assertEqual(payload["text"], "你好。")
        self.assertEqual(payload["language"], "chinese")
        self.assertIn("00:00:00,000", payload["srt"])
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(self.transcriber.calls[0]["body"], b"RIFFfake")
        self.assertEqual(self.transcriber.calls[0]["filename"], "sample.wav")

    def test_text_and_srt_formats_have_standard_media_types(self):
        text_response = self.post(suffix="&response_format=text")
        self.assertEqual(text_response.status_code, 200)
        self.assertTrue(text_response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(text_response.text, "你好。\n")

        srt_response = self.post(suffix="&response_format=srt")
        self.assertEqual(srt_response.status_code, 200)
        self.assertTrue(
            srt_response.headers["content-type"].startswith("application/x-subrip")
        )
        self.assertIn("attachment", srt_response.headers["content-disposition"])

    def test_upload_limit_is_enforced_while_streaming(self):
        response = self.post(body=b"x" * 1025)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.transcriber.calls)

    def test_multipart_is_rejected_with_actionable_contract(self):
        response = self.post(headers={"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(response.status_code, 415)
        self.assertIn("not multipart", response.json()["detail"])

    def test_failed_auth_uses_the_same_429_policy(self):
        wrong = {"Authorization": f"Bearer {'x' * 16}"}
        for _ in range(4):
            self.assertEqual(self.client.get("/readyz", headers=wrong).status_code, 401)
        response = self.client.get("/readyz", headers=wrong)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "300")

    def test_successful_auth_clears_failure_record(self):
        wrong = {"Authorization": f"Bearer {'x' * 16}"}
        for _ in range(4):
            self.client.get("/readyz", headers=wrong)
        self.assertEqual(self.client.get("/readyz", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get("/readyz", headers=wrong).status_code, 401)

    def test_openapi_documents_binary_body_and_bearer_auth(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/v1/transcriptions"]["post"]
        self.assertIn("application/octet-stream", operation["requestBody"]["content"])
        self.assertTrue(operation["security"])
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])


class APIConfigTests(unittest.TestCase):
    def test_missing_and_short_token_fail_closed(self):
        with self.assertRaises(SecurityConfigError):
            APIConfig.from_env({})
        with self.assertRaises(SecurityConfigError):
            APIConfig(token="x" * 15).validate()

    def test_all_components_use_the_same_environment_token(self):
        config = APIConfig.from_env({"CAPSWRITER_TOKEN": TOKEN})
        self.assertEqual(config.token, TOKEN)
        self.assertEqual(config.port, 6018)
        self.assertEqual(config.server_url, "ws://127.0.0.1:6016/asr")


class SRTTests(unittest.TestCase):
    def test_srt_falls_back_to_a_single_timed_segment_without_tokens(self):
        result = TranscriptionResult(
            task_id="task",
            text="fallback",
            text_accu="fallback",
            duration_seconds=2.0,
            processing_seconds=1.0,
            tokens=[],
            timestamps=[],
        )
        rendered = render_srt(result)
        self.assertIn("00:00:00,000 --> 00:00:02,000", rendered)
        self.assertIn("fallback", rendered)


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_wait_times_out_with_429(self):
        config = APIConfig(token=TOKEN, queue_timeout_seconds=0.01)

        async def ready():
            return {"ready": True}

        runtime = APIRuntime(
            config,
            transcriber=StubTranscriber(),
            readiness_probe=ready,
            auth_sleep=no_sleep,
        )
        await runtime.acquire()
        try:
            with self.assertRaises(HTTPException) as caught:
                await runtime.acquire()
            self.assertEqual(caught.exception.status_code, 429)
        finally:
            runtime.release()


if __name__ == "__main__":
    unittest.main()
