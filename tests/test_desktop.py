"""Focused checks for the native desktop API boundary and launcher."""

from io import BytesIO
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from harness_prototype.desktop import (
    ConversationView, DesktopApiClient, DesktopApiError, FluxDesktop, PermissionView, SkillPanel, desktop_main,
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

    @patch.object(DesktopApiClient, "_request")
    def test_skill_operations_send_only_ids_and_explicit_confirmation(self, request: MagicMock) -> None:
        """Use workspace discovery and conversation-bound skill changes without raw contents."""
        client = DesktopApiClient(("127.0.0.1", 4321))
        client.skills(Path("workspace with spaces"))
        self.assertIn("workspace+with+spaces", request.call_args.args[1])
        client.skills(Path("."), "chat/one")
        request.assert_called_with("GET", "/conversations/chat%2Fone/skills")
        client.select_skills("one", ["pirate"], True)
        request.assert_called_with("PATCH", "/conversations/one/skills",
                                   {"active_skills": ["pirate"], "confirm_context_reset": True})
        client.save_skill_preferences(Path("."), [])
        request.assert_called_with("PATCH", "/skill-preferences", {"workspace": ".", "active_skills": []})
        client.create_conversation("model", Path("."), ["pirate"])
        request.assert_called_with("POST", "/conversations",
                                   {"model": "model", "workspace": ".", "active_skills": ["pirate"]})

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
        self.assertNotIn("Permission required", text)
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
        desktop._permissions = {}
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


class DynamicSkillWidgetTests(unittest.TestCase):
    """Cover lightweight inline selection and slash-command routing without a model."""

    def setUp(self) -> None:
        """Create an isolated native fixture and prevent backend worker execution."""
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        self.addCleanup(self.destroy_root)
        self.tasks = patch.object(FluxDesktop, "_run_task").start()
        self.addCleanup(patch.stopall)
        self.history = patch.object(FluxDesktop, "_load_history").start()
        self.client = MagicMock()
        self.desktop = FluxDesktop(self.root, self.client, lambda: None, Path.cwd())
        self.desktop.models = ["test-model"]
        self.desktop._select_conversation({"conversation_id": "one", "active_skills": []})
        self.desktop.views["one"].recover([])
        self.catalog = {"conversation_id": "one", "active_skills": [], "skills": [
            {"id": "fixture.valid", "name": "Fixture", "description": "Harmless fixture",
             "format_version": 1, "manifest_path": "fixture/skill.json", "available": True},
            {"id": "fixture.missing", "name": "Missing", "available": False,
             "error": "Unavailable capabilities: absent."}], "limits": {"max_instructions_bytes": 32768}}

    def destroy_root(self) -> None:
        """Release scheduled callbacks before destroying the fixture interpreter."""
        self.root.update_idletasks()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_new_conversation_skips_picker_and_ignores_skill_defaults(self) -> None:
        """Create normally with an explicit empty skill list and no popup window."""
        self.client.settings.return_value = {"default_model": "test-model"}
        self.desktop.create_conversation()
        self.tasks.call_args.args[0]()
        self.client.create_conversation.assert_called_once_with("test-model", Path.cwd(), [])
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()))
        self.assertIsNone(self.desktop._skill_panel)

    def test_slash_activation_and_clear_preserve_view_and_never_reach_model(self) -> None:
        """Route /skill commands locally and apply an exact set without resetting context."""
        view = self.desktop.views["one"]
        self.desktop.send_turn("/skill fixture.valid, another")
        work, complete, _failed = self.tasks.call_args.args
        self.client.select_skills.return_value = {"conversation_id": "one", "active_skills": ["fixture.valid", "another"]}
        complete(work())
        self.client.select_skills.assert_called_once_with("one", ["fixture.valid", "another"], False)
        self.assertIs(self.desktop.views["one"], view)
        self.assertIn("fixture.valid", self.desktop.skills_text.get())
        self.desktop.send_turn("/skill")
        self.tasks.call_args.args[0]()
        self.client.select_skills.assert_called_with("one", [], False)
        self.client.stream_turn.assert_not_called()

    def test_inline_keyboard_selection_metadata_and_close_restore_focus(self) -> None:
        """Inspect with arrows and toggle using Space without opening a selection popup."""
        self.desktop.send_turn("/skills")
        self.tasks.call_args.args[1](self.catalog)
        panel = self.desktop._skill_panel
        self.assertIsInstance(panel, SkillPanel)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()))
        self.assertIn("Format version: 1", panel.details.get("1.0", tk.END))
        self.root.update()
        panel.options.focus_force()
        self.root.update()
        panel.options.event_generate("<space>")
        self.root.update()
        self.tasks.call_args.args[0]()
        self.client.select_skills.assert_called_once_with("one", ["fixture.valid"], False)
        panel.options.event_generate("<Down>")
        self.root.update()
        self.assertIn("Unavailable capabilities", panel.details.get("1.0", tk.END))
        panel.options.event_generate("<Return>")
        self.root.update()
        self.assertIn("Unavailable capabilities", panel.notice.get())
        self.desktop.close_skills()
        self.root.update()
        self.assertIsNone(self.desktop._skill_panel)
        self.assertEqual(self.root.focus_get(), self.desktop.message_input)

    def test_activation_keeps_owner_and_failure_never_retries(self) -> None:
        """Keep activation bound to the originating chat after navigation or disconnect."""
        self.desktop.send_turn("/skill fixture.valid")
        work, complete, failed = self.tasks.call_args.args
        self.desktop._select_conversation({"conversation_id": "two", "active_skills": []})
        self.desktop.views["two"].recover([])
        self.client.select_skills.return_value = {"conversation_id": "one", "active_skills": ["fixture.valid"]}
        complete(work())
        self.assertEqual(self.desktop.active_conversation_id, "two")
        self.assertEqual(self.desktop.skills_text.get(), "Active skills · None")
        self.desktop.send_turn("/skill fixture.valid")
        failed = self.tasks.call_args.args[2]
        failed(DesktopApiError("Backend unavailable. Reconnect to inspect saved skills."))
        self.assertFalse(self.desktop._skill_changing)
        self.assertIn("Reconnect", self.desktop.detail_text.get())
        self.assertEqual(self.client.select_skills.call_count, 1)
        self.client.stream_turn.assert_not_called()

    def test_busy_activation_and_late_inspector_responses_are_ignored(self) -> None:
        """Prevent racing turns and keep closed inline controls from reopening asynchronously."""
        self.desktop.open_skills()
        loaded = self.tasks.call_args.args[1]
        self.desktop.close_skills()
        loaded(self.catalog)
        self.assertIsNone(self.desktop._skill_panel)
        self.tasks.reset_mock()
        self.desktop.views["one"].active_turn_id = "turn"
        self.desktop.send_turn("/skill fixture.valid")
        self.tasks.assert_not_called()
        self.client.select_skills.assert_not_called()


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

    def test_permission_keyboard_default_redaction_and_dismissal(self) -> None:
        """Select the least broad grant and deny window dismissal without payload leaks."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(root.destroy)
        decide = MagicMock()
        event = {"operation": "apply_file_changes", "category": "write", "scope": "file: sample.txt",
                 "choices": ["deny", "allow_durable", "allow_session", "allow_once"],
                 "arguments": {"content": "private-secret"}, "revision": "private-secret"}
        dialog = PermissionView(root, event, decide)
        self.assertEqual(dialog.choices[dialog.options.curselection()[0]], "allow_once")
        labels = [widget.cget("text") for widget in dialog.options.master.winfo_children()
                  if widget.winfo_class() == "TLabel"]
        review = next(widget for widget in dialog.options.master.winfo_children() if isinstance(widget, ttk.Frame))
        labels.append(next(widget for widget in review.winfo_children() if isinstance(widget, tk.Text)).get("1.0", tk.END))
        self.assertNotIn("private-secret", "".join(labels))
        self.assertIn("file: sample.txt", "".join(labels))
        root.deiconify()
        root.update()
        dialog.options.focus_force()
        root.update()
        dialog.options.event_generate("<Down>")
        root.update()
        self.assertEqual(dialog.choices[dialog.options.curselection()[0]], "allow_session")
        dialog.options.event_generate("<Up>")
        root.update()
        dialog.options.event_generate("<space>")
        root.update()
        decide.assert_called_once_with("allow_once")
        dialog.confirm("allow_durable")
        decide.assert_called_once()
        denied = MagicMock()
        second = PermissionView(root, event, denied)
        root.tk.call(second.window.protocol("WM_DELETE_WINDOW"))
        denied.assert_called_once_with("deny")
        entered = MagicMock()
        third = PermissionView(root, event, entered)
        root.update()
        third.options.focus_force()
        root.update()
        third.options.event_generate("<Return>")
        root.update()
        entered.assert_called_once_with("allow_once")
        oversized = PermissionView(root, {**event, "scope": "x" * 12001}, MagicMock())
        self.assertEqual(oversized.choices, ["deny"])
        oversized.dismiss()

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_permission_routing_disconnect_and_receipt(
        self, _history: MagicMock, tasks: MagicMock,
    ) -> None:
        """Decide the original request despite navigation and deny after a lost stream."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(root.destroy)
        client = MagicMock()
        desktop = FluxDesktop(root, client, lambda: None, Path.cwd())
        for owner in ("one", "two"):
            desktop._select_conversation({"conversation_id": owner})
        started = {"conversation_id": "one", "turn_id": "turn", "sequence": 1, "type": "turn_started"}
        desktop._turn_event("one", started)
        requested = {**started, "sequence": 2, "type": "permission_request",
                     "permission_request_id": "permission", "choices": ["allow_once", "deny"]}
        desktop._turn_event("one", requested)
        desktop._permissions["permission"][2].choose_option()
        tasks.call_args.args[0]()
        client.resolve_permission.assert_called_once_with("permission", "allow_once")
        desktop._show_permission("one", requested)
        self.assertNotIn("permission", desktop._permissions)
        requested = {**requested, "sequence": 3, "permission_request_id": "second"}
        desktop._turn_event("one", requested)
        desktop._turn_error("one", DesktopApiError("Disconnected"))
        tasks.call_args.args[0]()
        client.resolve_permission.assert_called_with("second", "deny")
        self.assertFalse(desktop._permissions)
        receipt = {**started, "sequence": 4, "type": "permission_receipt",
                   "decision": "deny", "scope": "sample.txt", "arguments": "private-secret"}
        desktop._turn_event("one", receipt)
        rows = desktop.views["one"].render()
        self.assertTrue(any(tag == "status" and "Permission · Deny" in text for tag, text in rows))
        self.assertNotIn("private-secret", "".join(text for _, text in rows))

    @patch.object(FluxDesktop, "_run_task")
    def test_settings_save_and_revoke_use_captured_backend_values(self, tasks: MagicMock) -> None:
        """Persist preferences and revoke only the selected durable grant through the API."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(root.destroy)
        client = MagicMock()
        desktop = FluxDesktop(root, client, lambda: None, Path.cwd())
        desktop.models = ["test-model"]
        desktop._settings_loaded(({"default_model": "test-model", "backend_host": "http://localhost:11434"},
                                  [{"grant_id": "grant", "kind": "file", "target": "sample.txt", "workspace": "."}]))
        window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
        widgets = window.winfo_children()[0].winfo_children()
        next(widget for widget in widgets if isinstance(widget, ttk.Button)
             and widget.cget("text") == "Save preferences").invoke()
        tasks.call_args.args[0]()
        client.save_settings.assert_called_once_with("test-model", "http://localhost:11434")
        next(widget for widget in widgets if isinstance(widget, tk.Listbox)).selection_set(0)
        next(widget for widget in widgets if isinstance(widget, ttk.Button)
             and widget.cget("text") == "Revoke selected grant").invoke()
        tasks.call_args.args[0]()
        client.revoke_grant.assert_called_once_with("grant")


if __name__ == "__main__":
    unittest.main()
