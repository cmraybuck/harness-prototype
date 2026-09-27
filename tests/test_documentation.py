"""Mocked-network checks for bounded documentation retrieval."""

import unittest
from email.message import Message
from io import BytesIO
from types import TracebackType
from typing import Any
from unittest.mock import patch
from urllib.error import URLError

from harness_prototype.documentation import (
    DOCUMENTATION_TIMEOUT_SECONDS,
    MAX_DOCUMENTATION_BYTES,
    MAX_DOCUMENTATION_CHARACTERS,
    fetch_documentation,
)


class FakeResponse:
    """Provide the small urllib response interface used by the fetcher."""

    def __init__(self, body: bytes, url: str, content_type: str = "text/html") -> None:
        """Store response bytes, final URL, and MIME headers."""
        self._stream = BytesIO(body)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> "FakeResponse":
        """Return this response from a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the response stream."""
        self._stream.close()

    def geturl(self) -> str:
        """Return the final response URL."""
        return self._url

    def read(self, size: int = -1) -> bytes:
        """Read at most the requested response bytes."""
        return self._stream.read(size)


class FakeOpener:
    """Record bounded opener calls and return a configured outcome."""

    def __init__(self, outcome: FakeResponse | Exception) -> None:
        """Store a response or exception for the next request."""
        self.outcome = outcome
        self.timeout = None

    def open(self, request: Any, timeout: int | None = None) -> FakeResponse:
        """Return or raise the configured network outcome."""
        self.timeout = timeout
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class DocumentationTests(unittest.TestCase):
    """Cover URL policy, extraction, bounds, and structured failures."""

    def test_extracts_attributed_readable_html_with_timeout(self) -> None:
        """Return visible HTML text, final attribution, and retrieval metadata."""
        response = FakeResponse(
            b"<html><h1>API Guide</h1><script>secret()</script><p>Use HTTPS.</p></html>",
            "https://docs.example.com/guide",
        )
        opener = FakeOpener(response)
        with patch("harness_prototype.documentation.build_opener", return_value=opener):
            result = fetch_documentation("https://docs.example.com/guide")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_url"], "https://docs.example.com/guide")
        self.assertEqual(result["text"], "API Guide\nUse HTTPS.")
        self.assertNotIn("secret", result["text"])
        self.assertTrue(result["retrieved_at"].endswith("+00:00"))
        self.assertEqual(opener.timeout, DOCUMENTATION_TIMEOUT_SECONDS)

    def test_rejects_invalid_schemes_and_blocked_hosts_without_network(self) -> None:
        """Deny non-HTTPS and local-network destinations before opening them."""
        for url in (
            "http://docs.example.com", "file:///etc/passwd", "https://bad host/docs",
            "https://localhost/docs",
            "https://127.0.0.1/docs", "https://10.0.0.1/docs", "https://[::1]/docs",
        ):
            with self.subTest(url=url), patch(
                "harness_prototype.documentation.build_opener",
            ) as build:
                result = fetch_documentation(url)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "invalid_url")
                build.assert_not_called()

    def test_marks_byte_and_character_truncation(self) -> None:
        """Read only one byte past the cap and clearly mark bounded output."""
        response = FakeResponse(
            b"x" * (MAX_DOCUMENTATION_BYTES + 1),
            "https://docs.example.com/large",
            "text/plain; charset=utf-8",
        )
        with patch(
            "harness_prototype.documentation.build_opener",
            return_value=FakeOpener(response),
        ):
            result = fetch_documentation("https://docs.example.com/large")
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["text"]), MAX_DOCUMENTATION_CHARACTERS)
        self.assertEqual(result["bytes_read"], MAX_DOCUMENTATION_BYTES)

    def test_network_and_parsing_failures_are_structured(self) -> None:
        """Distinguish transport failures and unsupported response formats."""
        cases = (
            (URLError("offline"), "network_error"),
            (FakeResponse(b"binary", "https://docs.example.com/a.pdf", "application/pdf"),
             "unsupported_content_type"),
            (FakeResponse(b"\xff", "https://docs.example.com/bad", "text/plain; charset=utf-8"),
             "parsing_error"),
        )
        for outcome, status in cases:
            with self.subTest(status=status), patch(
                "harness_prototype.documentation.build_opener",
                return_value=FakeOpener(outcome),
            ):
                result = fetch_documentation("https://docs.example.com/reference")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
