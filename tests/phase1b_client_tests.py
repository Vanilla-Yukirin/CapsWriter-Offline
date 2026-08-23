# coding: utf-8
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capswriter_plus.api.client import APIClientError, _endpoint, transcribe_file


TOKEN = "phase1b-client-token-123456"


class FakeResponse:
    status = 200

    def read(self):
        return b'{"text":"ok"}'

    def getheader(self, name, default=None):
        if name.lower() == "content-type":
            return "application/json"
        return default


class FakeConnection:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_path = None
        self.headers = {}
        self.chunks = []
        self.closed = False
        self.__class__.instances.append(self)

    def putrequest(self, method, path):
        self.method = method
        self.request_path = path

    def putheader(self, name, value):
        self.headers[name] = value

    def endheaders(self):
        pass

    def send(self, chunk):
        self.chunks.append(chunk)

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


class APIClientTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances.clear()

    def test_upload_streams_file_and_uses_shared_token_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "voice sample.wav"
            media.write_bytes(b"abcdefgh")
            with patch(
                "capswriter_plus.api.client.http.client.HTTPConnection",
                FakeConnection,
            ):
                body, content_type = transcribe_file(
                    media,
                    token=TOKEN,
                    api_url="http://api.example:6018",
                    response_format="json",
                    language="chinese",
                    chunk_size=3,
                )

        connection = FakeConnection.instances[0]
        self.assertEqual((connection.host, connection.port), ("api.example", 6018))
        self.assertEqual(connection.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(connection.headers["Content-Length"], "8")
        self.assertEqual(connection.chunks, [b"abc", b"def", b"gh"])
        self.assertIn("filename=voice+sample.wav", connection.request_path)
        self.assertNotIn(TOKEN, connection.request_path)
        self.assertTrue(connection.closed)
        self.assertEqual(body, b'{"text":"ok"}')
        self.assertEqual(content_type, "application/json")

    def test_https_and_optional_base_path_are_supported(self):
        scheme, host, port, path = _endpoint(
            "https://example.test/caps",
            {"filename": "a.wav"},
        )
        self.assertEqual((scheme, host, port), ("https", "example.test", 443))
        self.assertEqual(path, "/caps/v1/transcriptions?filename=a.wav")

    def test_url_credentials_query_and_non_http_schemes_are_rejected(self):
        for value in (
            "ws://example.test",
            "https://user:pass@example.test",
            "https://example.test?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _endpoint(value, {"filename": "a.wav"})

    def test_missing_file_fails_before_connecting(self):
        with self.assertRaises(APIClientError):
            transcribe_file(Path("missing.wav"), token=TOKEN)


if __name__ == "__main__":
    unittest.main()
