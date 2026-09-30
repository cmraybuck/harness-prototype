"""Focused checks for the native desktop API boundary and launcher."""

from io import BytesIO
import json
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from harness_prototype.desktop import (
    ConversationView, DesktopApiClient, DesktopApiError, FluxDesktop, desktop_main,
)


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


class ConversationViewTests(unittest.TestCase):
    """Exercise ordered recovery and safe rendering independently of a display."""

    def event(self, sequence: int, kind: str, turn: str = "turn", **fields: object) -> dict:
        """Build an identified public event for one simulated conversation."""
        return {"conversation_id": "one", "turn_id": turn, "sequence": sequence,
                "type": kind, **fields}

    def test_recovery_deduplicates_stream_and_completed_history(self) -> None:
        """Keep one user message and assistant chunk across repeated recovery."""
        view = ConversationView()
        view.pending_message = "Inspect files"
        events = [self.event(1, "turn_started"), self.event(2, "assistant_text", text="Hello"),
                  self.event(3, "completion", outcome="success")]
        for event in events:
            view.accept(event)
        turn = {"turn_id": "turn", "status": "success", "user_prompt": "Inspect files",
                "events": [], "public_events": events}
        view.recover([turn])
        view.recover([turn])
        text = "".join(text for _, text in view.render())
        self.assertEqual(text.count("Inspect files"), 1)
        self.assertEqual(text.count("Hello"), 1)
        self.assertFalse(view.sending)
        self.assertIsNone(view.active_turn_id)

    def test_stale_running_snapshot_cannot_reactivate_completed_turn(self) -> None:
        """Prevent a racing history read from reverting a terminal event."""
        view = ConversationView()
        view.accept(self.event(1, "turn_started"))
        view.accept(self.event(2, "completion", outcome="model_error"))
        view.recover([{"turn_id": "turn", "status": "running", "user_prompt": "Hello",
                       "events": []}])
        self.assertIsNone(view.active_turn_id)
        self.assertEqual(view.turns["turn"]["status"], "model_error")

    def test_history_during_submission_preserves_reservation(self) -> None:
        """Do not enable a second send before the first turn ID arrives."""
        view = ConversationView()
        view.transport_active = True
        view.sending = True
        view.pending_message = "Hello"
        view.recover([])
        self.assertTrue(view.sending)
        self.assertEqual(view.pending_message, "Hello")

    def test_reconnect_running_then_interrupted_backend_turn(self) -> None:
        """Recover activity and permit continuation after backend interruption."""
        view = ConversationView()
        turn = {"turn_id": "turn", "status": "running", "user_prompt": "Hello",
                "events": [{"public_event": self.event(1, "turn_started")}]}
        view.recover([turn])
        self.assertEqual(view.active_turn_id, "turn")
        view.recover([{**turn, "status": "interrupted"}])
        self.assertFalse(view.sending)
        self.assertIn("follow-up", "".join(text for _, text in view.render()))

    def test_results_and_permissions_hide_raw_payloads_but_keep_paging(self) -> None:
        """Bound summaries and expose continuation without file or credential dumps."""
        view = ConversationView()
        view.accept(self.event(1, "turn_started"))
        view.accept(self.event(2, "tool_result", name="read_file", result={
            "ok": True, "truncated": True, "next_line": 42,
            "content": "secret" * 10000, "revision": "private", "arguments": "token",
        }))
        view.accept(self.event(3, "permission_request", arguments={"password": "secret"}))
        text = "".join(text for _, text in view.render())
        self.assertLess(len(text), 1000)
        self.assertIn('"next_line": 42', text)
        self.assertIn("Permission required", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)

    def test_failure_exhaustion_and_code_are_accessibly_labelled(self) -> None:
        """Show code as fixed-width text and explain retained-result recovery."""
        view = ConversationView()
        view.accept(self.event(1, "turn_started"))
        view.accept(self.event(2, "assistant_text", text="```python\nprint(1)\n```\n"))
        view.accept(self.event(3, "failure", message="Round limit reached"))
        view.accept(self.event(4, "completion", outcome="tool_round_limit"))
        rows = view.render()
        self.assertIn(("code", "print(1)\n"), rows)
        self.assertIn("Round limit", "".join(text for _, text in rows))
        self.assertIn("retained results", "".join(text for _, text in rows))

    def test_code_fences_split_across_chunks_keep_code_style(self) -> None:
        """Style fenced code even when model tokens split its delimiters."""
        view = ConversationView()
        view.accept(self.event(1, "turn_started"))
        view.accept(self.event(2, "assistant_text", text="`"))
        view.accept(self.event(3, "assistant_text", text="``python\nprint(1)\n```"))
        self.assertIn(("code", "print(1)\n"), view.render())

    def test_retry_rejects_turns_with_possible_side_effects(self) -> None:
        """Offer explicit retry only for model failures before tool activity."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        view = ConversationView()
        view.turns["turn"] = {"status": "model_error", "user_prompt": "Fix bug"}
        self.assertEqual(desktop._retry_message(view), "Fix bug")
        view.accept(self.event(1, "tool_call", name="apply_file_changes"))
        self.assertIsNone(desktop._retry_message(view))
        view.events.clear()
        view.turns["turn"]["events"] = [{"type": "tool_result", "result": {"ok": True}}]
        self.assertIsNone(desktop._retry_message(view))

    def test_cancellation_captures_selected_ids_and_views_are_independent(self) -> None:
        """Keep navigation from changing a queued cancellation's target."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.client = MagicMock()
        desktop.active_conversation_id = "one"
        first, second = ConversationView(), ConversationView()
        first.accept(self.event(1, "turn_started", turn="first"))
        second.accept(self.event(1, "turn_started", turn="second"))
        desktop.views = {"one": first, "two": second}
        queued = []
        desktop._run_task = lambda work: queued.append(work)
        desktop.cancel_turn()
        desktop.active_conversation_id = "two"
        queued[0]()
        desktop.client.cancel_turn.assert_called_once_with("one", "first")
        first.accept(self.event(2, "completion", turn="first", outcome="cancelled"))
        self.assertEqual(second.active_turn_id, "second")


class DesktopTurnClientTests(unittest.TestCase):
    """Cover transport event delivery, EOF failures, and paged recovery."""

    @patch("harness_prototype.desktop.http.client.HTTPConnection")
    def test_stream_delivers_events_and_closes_connection(self, connection_type: MagicMock) -> None:
        """Read complete UTF-8 NDJSON in order and release the transport."""
        events = [{"type": "assistant_text", "text": "café"}, {"type": "completion"}]
        response = connection_type.return_value.getresponse.return_value.__enter__.return_value
        response.status = 200
        response.readline.side_effect = [json.dumps(event).encode("utf-8") + b"\n" for event in events] + [b""]
        received = []
        DesktopApiClient(("127.0.0.1", 4321)).stream_turn("one", "Hello", received.append)
        self.assertEqual(received, events)
        connection_type.return_value.close.assert_called_once_with()

    @patch("harness_prototype.desktop.http.client.HTTPConnection")
    def test_premature_eof_requires_recovery(self, connection_type: MagicMock) -> None:
        """Never represent a disconnected turn as successfully completed."""
        response = connection_type.return_value.getresponse.return_value.__enter__.return_value
        response.status = 200
        response.readline.return_value = b""
        with self.assertRaisesRegex(DesktopApiError, "Reconnect"):
            DesktopApiClient(("127.0.0.1", 4321)).stream_turn("one", "Hello", lambda event: None)

    @patch("harness_prototype.desktop.urlopen")
    def test_history_reads_all_pages(self, opener: MagicMock) -> None:
        """Follow the backend cursor without duplicating previous pages."""
        opener.side_effect = [ResponseFixture(b'{"turns":[{"turn_id":"a"}],"next_cursor":50}'),
                              ResponseFixture(b'{"turns":[{"turn_id":"b"}],"next_cursor":null}')]
        turns = DesktopApiClient(("127.0.0.1", 4321)).history("one")
        self.assertEqual([turn["turn_id"] for turn in turns], ["a", "b"])
        self.assertIn("after=50", opener.call_args_list[1].args[0].full_url)

    @patch("harness_prototype.desktop.http.client.HTTPConnection")
    def test_oversized_event_is_rejected(self, connection_type: MagicMock) -> None:
        """Stop a stream that exceeds its per-event transport bound."""
        response = connection_type.return_value.getresponse.return_value.__enter__.return_value
        response.status = 200
        response.readline.return_value = b"x" * (2 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(DesktopApiError, "transport limit"):
            DesktopApiClient(("127.0.0.1", 4321)).stream_turn("one", "Hello", lambda event: None)


class DesktopWidgetTests(unittest.TestCase):
    """Check native widgets and selected-view routing when Tk is available."""

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_widgets_route_background_events_and_enable_continuation(
        self, _history: MagicMock, _tasks: MagicMock,
    ) -> None:
        """Keep the visible transcript and cancellation scoped to the selected view."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(root.destroy)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        for conversation_id in ("one", "two"):
            desktop._select_conversation({"conversation_id": conversation_id, "title": conversation_id})
            desktop.views[conversation_id].recover([])
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn",
                                   "sequence": 1, "type": "turn_started"})
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn",
                                   "sequence": 2, "type": "assistant_text", "text": "Background reply"})
        self.assertNotIn("Background reply", desktop.transcript.get("1.0", tk.END))
        desktop._select_conversation({"conversation_id": "one", "title": "one"})
        self.assertIn("Background reply", desktop.transcript.get("1.0", tk.END))
        self.assertEqual(str(desktop.cancel_button["state"]), "normal")
        self.assertEqual(str(desktop.send_button["state"]), "disabled")
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn",
                                   "sequence": 3, "type": "completion", "outcome": "cancelled"})
        self.assertEqual(str(desktop.send_button["state"]), "normal")
        self.assertEqual(str(desktop.cancel_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
