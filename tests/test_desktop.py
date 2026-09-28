"""Focused checks for the native desktop API boundary and launcher."""

from io import BytesIO
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from harness_prototype.desktop import DesktopApiClient, DesktopApiError, desktop_main


class ResponseFixture:
    """Provide the context-manager subset returned by urlopen."""

    def __init__(self, body: bytes) -> None:
        """Store one encoded response body."""
        self.body = body

    def __enter__(self) -> "ResponseFixture":
        """Return this response when entering the request context."""
        return self

    def __exit__(self, *args: object) -> None:
        """Leave the response context without suppressing failures."""

    def read(self) -> bytes:
        """Return the encoded response once requested."""
        return self.body


class DesktopApiClientTests(unittest.TestCase):
    """Cover portable GUI-to-backend requests and recovery errors."""

    @patch("harness_prototype.desktop.urlopen")
    def test_conversation_operations_use_versioned_backend_contract(
        self, opener: MagicMock,
    ) -> None:
        """Send creation and selection through JSON API calls only."""
        opener.side_effect = [
            ResponseFixture(b'{"conversation_id":"one","model":"alpha"}'),
            ResponseFixture(b'{"conversation_id":"one","model":"beta"}'),
        ]
        client = DesktopApiClient(("127.0.0.1", 4321))

        created = client.create_conversation("alpha", Path("C:/workspace"))
        changed = client.update_conversation("one", "model", "beta")

        self.assertEqual(created["conversation_id"], "one")
        self.assertEqual(changed["model"], "beta")
        first_request = opener.call_args_list[0].args[0]
        second_request = opener.call_args_list[1].args[0]
        self.assertEqual(first_request.full_url, "http://127.0.0.1:4321/v1/conversations")
        self.assertEqual(first_request.get_method(), "POST")
        self.assertEqual(second_request.get_method(), "PATCH")
        self.assertNotIn("ollama", first_request.full_url)

    @patch("harness_prototype.desktop.urlopen")
    def test_structured_backend_error_remains_actionable(self, opener: MagicMock) -> None:
        """Expose safe protocol guidance and its stable recovery code."""
        opener.side_effect = HTTPError(
            "http://127.0.0.1/v1/conversations",
            409,
            "Conflict",
            {},
            BytesIO(
                b'{"error":{"code":"model_configuration_required",'
                b'"message":"Configure HARNESS_MODELS."}}'
            ),
        )
        client = DesktopApiClient(("127.0.0.1", 4321))

        with self.assertRaisesRegex(DesktopApiError, "HARNESS_MODELS") as raised:
            client.list_conversations()
        self.assertEqual(raised.exception.code, "model_configuration_required")

    @patch("harness_prototype.desktop.urlopen", side_effect=URLError("offline"))
    def test_backend_outage_has_retry_guidance(self, _opener: MagicMock) -> None:
        """Translate connection failures into a recoverable desktop message."""
        client = DesktopApiClient(("127.0.0.1", 4321))
        with self.assertRaisesRegex(DesktopApiError, "Restart FLUX"):
            client.health()

    def test_invalid_selection_field_is_rejected_before_network_access(self) -> None:
        """Restrict desktop metadata changes to documented backend fields."""
        client = DesktopApiClient(("127.0.0.1", 4321))
        with self.assertRaisesRegex(ValueError, "model or workspace"):
            client.update_conversation("one", "title", "unsupported")


class DesktopLauncherTests(unittest.TestCase):
    """Cover managed backend cleanup when native window startup fails."""

    @patch("harness_prototype.desktop.tk.Tk", side_effect=tk.TclError("no display"))
    @patch("harness_prototype.desktop.DesktopServiceHost")
    def test_launcher_stops_backend_after_window_failure(
        self, host_type: MagicMock, _tk_type: MagicMock,
    ) -> None:
        """Release the private listener if the platform window cannot open."""
        host = host_type.return_value
        self.assertEqual(desktop_main(), 1)
        host.start.assert_called_once_with()
        host.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
