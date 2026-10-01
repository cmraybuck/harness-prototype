"""Focused checks for the native desktop API boundary and launcher."""

from io import BytesIO
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from harness_prototype.desktop_design import CAPTION_GLYPHS, COLORS, CommandPalette
from harness_prototype.desktop import (
    ConversationView, DecisionDialog, DecisionOptions, DesktopApiClient, DesktopApiError, FluxDesktop, PermissionView, SkillPanel, desktop_main,
)


def destroy_fixture_root(root: tk.Tk) -> None:
    """Drain theme geometry events and cancel timers before destroying a fixture interpreter."""
    root.update_idletasks()
    for callback in root.tk.call("after", "info"):
        root.after_cancel(callback)
    root.destroy()


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
        desktop._permission_queue = {}
        desktop.root = MagicMock()
        desktop._permission_notice = MagicMock()
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

    def test_command_palette_filters_navigates_completes_and_dismisses(self) -> None:
        """Browse descriptions without submitting and preserve ordinary slash input."""
        self.desktop.message_input.insert("1.0", "/")
        self.desktop._update_commands()
        self.assertEqual(len(self.desktop._command_matches), 9)
        self.assertIn("Start a fresh conversation", self.desktop.command_menu.choices[0][1])
        self.desktop._move_command(1)
        self.desktop._send_key(None)
        self.assertEqual(self.desktop.message_input.get("1.0", "end-1c"), "/model")
        self.assertFalse(self.desktop._command_matches)
        self.client.stream_turn.assert_not_called()
        self.desktop.message_input.delete("1.0", tk.END)
        self.desktop.message_input.insert("1.0", "/sta")
        self.desktop._update_commands()
        self.assertEqual(self.desktop._command_matches, ["/status"])
        self.desktop._hide_commands()
        self.desktop._update_commands()
        self.assertFalse(self.desktop._command_matches)
        for text in ("/unknown", "/tmp/file", "explain /help", "/skill pirate"):
            self.desktop.message_input.delete("1.0", tk.END)
            self.desktop.message_input.insert("1.0", text)
            self.desktop._update_commands()
            self.assertFalse(self.desktop._command_matches)

    def test_command_rows_share_app_typography_and_pointer_selection(self) -> None:
        """Keep rounded rows separated and follow text preferences without losing navigation."""
        self.desktop.message_input.insert("1.0", "/")
        self.desktop._update_commands()
        palette = self.desktop.command_menu
        shapes = [item for item in palette.canvas.find_all() if palette.canvas.type(item) == "polygon"]
        self.assertEqual(len(shapes), 9)
        self.assertEqual(palette.canvas.itemcget(shapes[0], "fill"), COLORS["selected"])
        self.desktop._presentation["text_size"] = "large"
        self.desktop._apply_typography()
        self.assertEqual(palette.title_font.actual("size"),
                         int(self.root.tk.call("font", "actual", "TkDefaultFont", "-size")))
        self.assertEqual(palette.title_font.actual("family"),
                         self.root.tk.call("font", "actual", "TkDefaultFont", "-family"))
        event = MagicMock()
        event.y = palette.row_height + 1
        palette._hover(event)
        self.assertEqual(palette.index, 1)
        palette._click(event)
        self.assertEqual(self.desktop.message_input.get("1.0", "end-1c"), "/model")
        self.client.stream_turn.assert_not_called()

    def test_help_and_status_format_local_results_without_model_calls(self) -> None:
        """Render command results using chat formatting and backend runtime facts."""
        self.desktop.send_turn("/help")
        self.assertIn("/help", self.desktop.transcript.get("1.0", tk.END))
        self.assertIn("/tool-activity", self.desktop.views["one"].command_results[-1][1])
        self.desktop.send_turn("/status")
        work, loaded, _failed = self.tasks.call_args.args
        self.client.conversation_status.return_value = {
            "model": "test-model", "server": "local", "workspace": "fixture",
            "message_count": 2, "context_used": 100, "context_limit": 1000,
            "active_groups": [], "active_skills": [], "character_counts": {"messages": 20},
            "tools": [{"name": "read_file", "description": "Read text"}]}
        loaded(work())
        self.client.conversation_status.assert_called_once_with("one")
        output = self.desktop.views["one"].command_results[-1][1]
        self.assertIn("**Retained messages:** 2", output)
        self.assertIn("read_file", output)
        self.client.stream_turn.assert_not_called()
        self.assertFalse(self.desktop.views["one"].turns)

    def test_status_failure_stays_in_chat_without_disconnect_ui(self) -> None:
        """Keep status-request errors local and preserve the existing connection indicator."""
        self.desktop.status_text.set("Backend connected · Ollama available")
        self.desktop.send_turn("/status")
        failed = self.tasks.call_args.args[2]
        with patch.object(self.desktop, "_show_error") as show_error:
            failed(DesktopApiError("Local request failed."))
        show_error.assert_not_called()
        self.assertEqual(self.desktop.status_text.get(), "Backend connected · Ollama available")
        self.assertIn("Command failed: Local request failed.",
                      self.desktop.views["one"].command_results[-1][1])
        self.client.stream_turn.assert_not_called()

    def test_clear_keeps_current_model_and_workspace(self) -> None:
        """Create a fresh conversation using current selections rather than defaults."""
        self.desktop.model_text.set("test-model")
        self.desktop.send_turn("/clear")
        self.tasks.call_args.args[0]()
        self.client.create_conversation.assert_called_once_with("test-model", Path.cwd(), [])
        self.client.stream_turn.assert_not_called()

    def test_unknown_slash_input_is_not_intercepted(self) -> None:
        """Leave paths and unknown commands eligible for normal model submission."""
        self.assertFalse(self.desktop._dispatch_command("one", "/unknown"))
        self.assertFalse(self.desktop._dispatch_command("one", "/tmp/file"))

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

    def test_skills_menu_preserves_conversation_scroll_on_repeated_open(self) -> None:
        """Keep the reading position when skill results are appended and the menu closes."""
        self.root.geometry("800x600")
        view = self.desktop.views["one"]
        for index in range(12):
            view.turns[str(index)] = {"user_prompt": "Earlier conversation text " * 80,
                                      "status": "success", "events": []}
        self.desktop._render_conversation()
        self.root.update()
        for fraction in (0.35, 1.0):
            self.desktop.transcript.yview_moveto(fraction)
            self.root.update_idletasks()
            position = self.desktop._viewport_pixels()
            self.assertGreater(position, 100)
            for _ in range(2):
                self.desktop.send_turn("/skills")
                self.tasks.call_args.args[1](self.catalog)
                self.root.update()
                self.assertAlmostEqual(self.desktop._viewport_pixels(), position, delta=1)
                self.desktop.close_skills()
                self.root.update()
                self.assertAlmostEqual(self.desktop._viewport_pixels(), position, delta=1)

    def test_inline_keyboard_toggle_and_close_restore_focus(self) -> None:
        """Browse compact rows and toggle with Space without opening a popup."""
        self.desktop.send_turn("/skills")
        self.tasks.call_args.args[1](self.catalog)
        panel = self.desktop._skill_panel
        self.assertIsInstance(panel, SkillPanel)
        self.assertIsInstance(panel, CommandPalette)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()))
        self.root.update()
        panel.canvas.focus_force()
        self.root.update()
        panel.canvas.event_generate("<space>")
        self.root.update()
        for request in self.tasks.call_args_list:
            request.args[0]()
        self.client.select_skills.assert_called_once_with("one", ["fixture.valid"], False)
        panel.canvas.event_generate("<Down>")
        self.root.update()
        panel.canvas.event_generate("<Return>")
        self.root.update()
        self.assertIn("Unavailable capabilities", panel.notice.get())
        panel.close_button.invoke()
        self.root.update()
        self.assertIsNone(self.desktop._skill_panel)
        self.assertEqual(self.root.focus_get(), self.desktop.message_input)

    def test_skill_status_buttons_and_compact_command_rows(self) -> None:
        """Use palette colors and concise rows with safe click activation."""
        activate = MagicMock()
        panel = SkillPanel(self.root, self.catalog, activate, MagicMock())
        self.root.geometry("800x600")
        panel.place(x=0, y=0, width=560)
        self.root.update()
        self.assertEqual(panel.canvas.cget("background"), COLORS["surface"])
        self.assertEqual(panel.buttons[0].cget("style"), "Selected.Skill.Danger.TButton")
        self.assertEqual(panel.buttons[0].cget("text"), "INACTIVE")
        self.assertEqual(panel.close_button.cget("text"), CAPTION_GLYPHS["close"])
        self.assertEqual(panel.close_button.cget("style"), "Close.Caption.TButton")
        text = "\n".join(panel.canvas.itemcget(item, "text") for item in panel.canvas.find_all()
                         if panel.canvas.type(item) == "text")
        self.assertIn("Fixture", text)
        self.assertIn("Harmless fixture", text)
        self.assertNotIn("manifest", text.lower())
        self.assertNotIn("Format version", text)
        self.assertLessEqual(panel.canvas.winfo_reqheight(), panel.row_height * 5)
        self.assertFalse(any(isinstance(widget, ttk.Scrollbar) for widget in panel.winfo_children()))
        panel.buttons[0].invoke()
        activate.assert_called_once_with(["fixture.valid"])
        activate.reset_mock()
        self.assertTrue(panel.buttons[1].instate(["disabled"]))
        panel.buttons[1].invoke()
        activate.assert_not_called()
        panel.destroy()

        panel = SkillPanel(self.root, {"skills": [], "active_skills": ["missing"]}, activate, MagicMock())
        self.assertEqual(panel.buttons[0].cget("text"), "ACTIVE")
        self.assertEqual(panel.buttons[0].cget("style"), "Selected.Skill.Success.TButton")
        self.assertFalse(panel.buttons[0].instate(["disabled"]))
        panel.buttons[0].invoke()
        activate.assert_called_once_with([])
        panel.destroy()

        panel = SkillPanel(self.root, {"skills": []}, activate, MagicMock())
        self.assertEqual(panel.buttons, [])
        self.assertEqual(panel.choose_option(), "break")
        panel.destroy()

    def test_long_skill_descriptions_keep_menu_height_bounded(self) -> None:
        """Keep large catalogs and descriptions within five compact visible rows."""
        catalog = {"skills": [{"id": str(index), "name": "Skill", "available": True,
                               "description": "A long readable description. " * 100}
                              for index in range(8)]}
        panel = SkillPanel(self.root, catalog, MagicMock(), MagicMock())
        self.root.geometry("800x600")
        panel.place(x=0, y=0, width=560)
        self.root.update()
        panel.redraw()
        descriptions = [panel.canvas.itemcget(item, "text") for item in panel.canvas.find_all()
                        if panel.canvas.type(item) == "text"][1::2]
        self.assertTrue(all(len(text.splitlines()) <= 2 for text in descriptions))
        self.assertTrue(all(text.endswith("…") for text in descriptions))
        self.assertEqual(panel.canvas.winfo_reqheight(), panel.row_height * 5)
        panel.move(7)
        self.assertEqual(panel.index, 7)
        self.assertGreater(panel.canvas.yview()[0], 0)
        panel.destroy()

    def test_skill_menu_respects_available_height_and_caption_hover(self) -> None:
        """Keep a larger catalog within a short chat and reuse the app caption states."""
        catalog = {"skills": [{"id": str(index), "name": "Skill", "available": True,
                               "description": "A concise description."} for index in range(8)]}
        panel = SkillPanel(self.root, catalog, MagicMock(), MagicMock(), max_height=160)
        self.root.geometry("800x600")
        panel.place(x=0, y=0, width=640)
        self.root.update()
        self.assertEqual(panel.canvas.winfo_height(), 160)
        self.assertLessEqual(panel.winfo_reqheight(), 270)
        self.assertEqual(panel.close_button.cget("style"), "Close.Caption.TButton")
        panel.close_button.state(["active"])
        self.assertEqual(ttk.Style(self.root).lookup("Close.Caption.TButton", "foreground", ("active",)),
                         "#ffffff")
        panel.move(7)
        self.root.update()
        self.assertGreater(panel.canvas.yview()[0], 0)
        panel.destroy()

    def test_skill_row_controls_and_text_do_not_overlap_at_text_sizes(self) -> None:
        """Keep buttons and text inside their rounded rows across app font sizes."""
        self.root.geometry("800x600")
        for size in ("small", "default", "large"):
            for width in (360, 640):
                with self.subTest(size=size, width=width):
                    self.desktop.theme.set_text_size(size)
                    panel = SkillPanel(self.root, self.catalog, MagicMock(), MagicMock())
                    panel.place(x=0, y=0, width=width)
                    self.root.update()
                    panel.redraw()
                    self.root.update_idletasks()
                    text_items = [item for item in panel.canvas.find_all()
                                  if panel.canvas.type(item) == "text"]
                    for index, button in enumerate(panel.buttons):
                        title = panel.canvas.bbox(text_items[index * 2])
                        description = panel.canvas.bbox(text_items[index * 2 + 1])
                        self.assertGreater(title[0], button.winfo_x() + button.winfo_width())
                        self.assertGreater(description[0], button.winfo_x() + button.winfo_width())
                        self.assertGreaterEqual(description[1], title[3])
                        self.assertLessEqual(description[3], (index + 1) * panel.row_height - 5)
                        self.assertLessEqual(description[2], panel.canvas.winfo_width() - 4)
                        self.assertGreaterEqual(button.winfo_y(), index * panel.row_height)
                        self.assertLessEqual(button.winfo_y() + button.winfo_height(),
                                             (index + 1) * panel.row_height - 5)
                    panel.destroy()

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
    def test_viewport_accepts_scalar_and_tuple_tk_pixel_counts(self) -> None:
        """Handle Tkinter's scalar update counts as well as tuple and empty counts."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.transcript = MagicMock()
        desktop.transcript.yview.return_value = (0.25, 0.75)
        for height in (1000, (1000,)):
            with self.subTest(height=height):
                desktop.transcript.count.return_value = height
                self.assertEqual(desktop._viewport_pixels(), 250)
                desktop._restore_viewport_pixels(100)
                desktop.transcript.yview_moveto.assert_called_with(0.1)
        for height in (0, None):
            with self.subTest(height=height):
                desktop.transcript.count.return_value = height
                self.assertEqual(desktop._viewport_pixels(), 0)
                desktop._restore_viewport_pixels(0)

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_activity_preserves_pixels_and_refocus_allows_scrolling(self, _history: MagicMock, _tasks: MagicMock) -> None:
        """Keep clipped bubbles stationary across activity and allow scrolling after refocus."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop._select_conversation({"conversation_id": "one"})
        view = desktop.views["one"]
        view.recover([])
        for index in range(8):
            view.turns[str(index)] = {"user_prompt": "Earlier prompt " * 80,
                                      "status": "success", "events": []}
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "active",
                                   "sequence": 1, "type": "turn_started"})
        root.deiconify()
        root.update()
        sequence = 1
        for fraction in (0.35, 1.0):
            desktop.transcript.yview_moveto(fraction)
            root.update_idletasks()
            position = desktop._viewport_pixels()
            for kind, extra in (("tool_call", {"name": "read_file", "tool_call_id": str(fraction)}),
                                ("tool_result", {"name": "read_file", "tool_call_id": str(fraction),
                                                 "result": {"ok": True, "content": "fixture"}}),
                                ("assistant_text", {"text": "New output\n" * 30})):
                sequence += 1
                desktop._turn_event("one", {"conversation_id": "one", "turn_id": "active",
                                           "sequence": sequence, "type": kind, **extra})
                root.update_idletasks()
                desktop._layout_changed()
                self.assertAlmostEqual(desktop._viewport_pixels(), position, delta=1)
        desktop.jump_to_latest()
        root.update_idletasks()
        self.assertAlmostEqual(desktop.transcript.yview()[1], 1.0, delta=0.001)
        desktop.transcript.event_generate("<FocusOut>")
        desktop.transcript.event_generate("<FocusIn>")
        position = desktop._viewport_pixels()
        desktop.transcript.yview_scroll(-1, "units")
        root.update_idletasks()
        scrolled = desktop._viewport_pixels()
        self.assertLess(scrolled, position)
        # Near-bottom layout must not snap back or repeatedly schedule itself.
        for _ in range(3):
            if desktop._layout_timer is not None:
                root.after_cancel(desktop._layout_timer)
            desktop._layout_changed()
            root.update_idletasks()
            self.assertAlmostEqual(desktop._viewport_pixels(), scrolled, delta=1)
        self.assertIsNone(desktop._layout_timer)

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_stream_updates_keep_completed_bubbles_and_wheel_scrolls_chat(self, _history: MagicMock, _tasks: MagicMock) -> None:
        """Keep prior selectable widgets stable and route bubble-wheel input to the conversation."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop._select_conversation({"conversation_id": "one"})
        view = desktop.views["one"]
        view.recover([])
        view.turns["completed"] = {"user_prompt": "Earlier prompt", "status": "success", "events": []}
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "active", "sequence": 1, "type": "turn_started"})
        root.deiconify()
        root.update()
        desktop._render_conversation()
        completed = desktop._transcript_widgets[0]
        bubble = next(child for child in completed.winfo_children() if isinstance(child, tk.Text))
        with patch.object(desktop.transcript, "yview_scroll") as scroll:
            bubble.event_generate("<MouseWheel>", delta=-120)
            root.update()
            scroll.assert_called_once_with(3, "units")
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "active", "sequence": 2, "type": "assistant_text", "text": "Streamed reply"})
        self.assertTrue(completed.winfo_exists())
        self.assertIs(desktop._transcript_widgets[0], completed)
        self.assertIn("Streamed reply", desktop.transcript.get("1.0", tk.END))

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_session_cards_keep_pin_and_menu_bound_to_their_owner(self, _history: MagicMock, tasks: MagicMock) -> None:
        """Operate another row without redirecting pin or archive actions to the active session."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop._select_conversation({"conversation_id": "one", "title": "First"})
        desktop._replace_conversations([{"conversation_id": "one", "title": "First", "pinned": True},
                                        {"conversation_id": "two", "title": "Second", "archived": False}], select=False)
        card, title, detail, pin, menu = desktop.conversation_list.rows["two"]
        self.assertEqual(title.cget("text"), "Second")
        self.assertEqual(card.cget("style"), "Session.TFrame")
        self.assertEqual(desktop.conversation_list.rows["one"][0].cget("style"), "SelectedSession.TFrame")
        pin.invoke()
        tasks.call_args.args[0]()
        desktop.client.session_state.assert_called_once_with("two", "pinned", True)
        menu.invoke()
        dropdown = desktop._session_dropdown
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in root.winfo_children()))
        self.assertEqual([choice[1] for choice in dropdown.choices], ["rename", "archive", "delete"])
        dropdown.move(1)
        dropdown.confirm(dropdown.index)
        tasks.call_args.args[0]()
        self.assertEqual(desktop.client.session_state.call_args.args, ("two", "archived", True))
        self.assertEqual(desktop.active_conversation_id, "one")

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_queued_stream_events_render_once_without_losing_text(self, _history: MagicMock, _tasks: MagicMock) -> None:
        """Apply all queued token events in order with one presentation update."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop._select_conversation({"conversation_id": "one"})
        desktop.views["one"].recover([])
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn", "sequence": 1, "type": "turn_started"})
        for sequence, text in enumerate(("First ", "second ", "third."), start=2):
            event = {"conversation_id": "one", "turn_id": "turn", "sequence": sequence, "type": "assistant_text", "text": text}
            desktop._ui_callbacks.put(lambda event=event: desktop._turn_event("one", event))
        with patch.object(desktop, "_render_turn", wraps=desktop._render_turn) as render:
            desktop._drain_ui_callbacks()
            render.assert_called_once()
        self.assertIn("First second third.", desktop.transcript.get("1.0", tk.END))

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
        self.addCleanup(destroy_fixture_root, root)
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
        self.assertEqual(desktop.send_button.cget("text"), "■")
        self.assertEqual(str(desktop.send_button["state"]), "normal")
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn",
                                   "sequence": 3, "type": "completion", "outcome": "cancelled"})
        self.assertEqual(str(desktop.send_button["state"]), "normal")
        self.assertEqual(str(desktop.cancel_button["state"]), "disabled")

    def test_session_decision_is_compact_and_uses_themed_actions(self) -> None:
        """Keep decisions content sized and confirm the selected rounded action."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.geometry("1000x700")
        self.addCleanup(destroy_fixture_root, root)
        decide = MagicMock()
        dialog = DecisionDialog(root, "Delete session?", "The saved draft will be deleted.",
                                [("Keep session", "keep"), ("Delete permanently", "delete")], decide)
        root.update()
        self.assertIsInstance(dialog.options, DecisionOptions)
        self.assertLess(dialog.window.winfo_width(), 600)
        self.assertLess(dialog.window.winfo_height(), 350)
        self.assertEqual(dialog.options.buttons[0].cget("style"), "Selected.Neutral.Inset.TButton")
        dialog.move(1)
        self.assertEqual(dialog.options.buttons[0].cget("style"), "Neutral.Inset.TButton")
        self.assertEqual(dialog.options.buttons[1].cget("style"), "Selected.Danger.Inset.TButton")
        dialog.options.buttons[1].invoke()
        decide.assert_called_once_with("delete")
        self.assertFalse(dialog.window.winfo_exists())

    def test_permission_keyboard_default_redaction_and_dismissal(self) -> None:
        """Select the least broad grant and deny window dismissal without payload leaks."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        decide = MagicMock()
        event = {"operation": "apply_file_changes", "category": "write", "scope": "file: sample.txt",
                 "choices": ["deny", "allow_durable", "allow_session", "allow_once"],
                 "arguments": {"content": "private-secret"}, "revision": "private-secret"}
        dialog = PermissionView(root, event, decide)
        self.assertIsInstance(dialog.options, DecisionOptions)
        self.assertEqual(dialog.choices[dialog.options.curselection()[0]], "allow_once")
        labels = [widget.cget("text") for widget in dialog.options.master.winfo_children()
                  if widget.winfo_class() == "TLabel"]
        review = next(widget for widget in dialog.body.frame.winfo_children() if isinstance(widget, ttk.Frame))
        labels.append(next(widget for widget in review.winfo_children() if isinstance(widget, tk.Text)).get("1.0", tk.END))
        self.assertEqual(dialog.options.buttons[0].cget("style"), "Selected.Success.Inset.TButton")
        self.assertEqual(next(widget for widget in review.winfo_children() if isinstance(widget, ttk.Scrollbar)).cget("style"),
                         "Chat.Vertical.TScrollbar")
        self.assertEqual(next(widget for widget in dialog.body.canvas.master.winfo_children()
                              if isinstance(widget, ttk.Scrollbar)).cget("style"), "Chat.Vertical.TScrollbar")
        self.assertNotIn("private-secret", "".join(labels))
        self.assertIn("file: sample.txt", "".join(labels))
        root.deiconify()
        root.update()
        self.assertLess(dialog.window.winfo_height(), 600)
        self.assertLess(dialog.window.winfo_width(), 700)
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
        self.assertIsInstance(second.window, ttk.Frame)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in root.winfo_children()))
        second.options.focus_force()
        root.update()
        second.options.event_generate("<Escape>")
        root.update()
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
    def test_rename_panel_keeps_draft_and_captured_session(self, _history: MagicMock, tasks: MagicMock) -> None:
        """Rename inside the app preserves the draft and submits the originating ID."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop.message_input.insert("1.0", "Unsent draft")
        desktop._rename_session("original", "Old title")
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in root.winfo_children()))
        panel = next(child for child in root.winfo_children() if isinstance(child, ttk.Frame)
                     and child.winfo_manager() == "place")
        root.update_idletasks()
        self.assertLess(panel.winfo_reqheight(), 300)
        self.assertLess(panel.winfo_reqwidth(), 600)
        frame = panel.winfo_children()[0]
        entry = next(child for child in frame.winfo_children() if isinstance(child, ttk.Entry))
        entry.delete(0, tk.END)
        entry.insert(0, "New title")
        desktop.active_conversation_id = "another"
        next(child for child in frame.winfo_children() if isinstance(child, ttk.Button)).invoke()
        tasks.call_args.args[0]()
        desktop.client.session_state.assert_called_once_with("original", "title", "New title")
        self.assertEqual(desktop.message_input.get("1.0", "end-1c"), "Unsent draft")
        self.assertFalse(panel.winfo_exists())

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
        self.addCleanup(destroy_fixture_root, root)
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
    @patch.object(FluxDesktop, "_load_history")
    def test_serialized_permissions_expire_and_keep_owner(self, _history: MagicMock, tasks: MagicMock) -> None:
        """Queue cross-session decisions and remove resolved entries before showing the next."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        for owner in ("one", "two"):
            desktop._select_conversation({"conversation_id": owner, "title": owner})
            desktop.views[owner].recover([])
            desktop._turn_event(owner, {"conversation_id": owner, "turn_id": owner, "sequence": 1, "type": "turn_started"})
            desktop._turn_event(owner, {"conversation_id": owner, "turn_id": owner, "sequence": 2, "type": "permission_request",
                                        "permission_request_id": owner, "choices": ["allow_once", "deny"]})
        self.assertEqual(len(desktop._permissions), 1)
        self.assertEqual(len(desktop._permission_queue), 1)
        self.assertEqual(desktop.active_conversation_id, "two")
        desktop._turn_event("two", {"conversation_id": "two", "turn_id": "two", "sequence": 3, "type": "permission_receipt", "permission_request_id": "two", "decision": "deny"})
        self.assertFalse(desktop._permission_queue)
        desktop._permissions["one"][2].choose_option()
        tasks.call_args.args[0]()
        desktop.client.resolve_permission.assert_called_once_with("one", "allow_once")
        desktop._present_next_permission()
        self.assertFalse(desktop._permissions)

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_drafts_navigation_enter_and_working_expansion(self, _history: MagicMock, _tasks: MagicMock) -> None:
        """Preserve drafts independently and retain Working expansion as text streams."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        desktop._select_conversation({"conversation_id": "one", "draft": "Restored draft"})
        desktop.views["one"].recover([])
        self.assertEqual(desktop.message_input.get("1.0", "end-1c"), "Restored draft")
        desktop.message_input.delete("1.0", tk.END)
        desktop.message_input.insert("1.0", "New unsent draft")
        desktop._select_conversation({"conversation_id": "two", "draft": "Second draft"})
        desktop.views["two"].recover([])
        desktop._select_conversation({"conversation_id": "one"})
        self.assertEqual(desktop.message_input.get("1.0", "end-1c"), "New unsent draft")
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn", "sequence": 1, "type": "turn_started"})
        desktop.views["one"].expanded_working.add("turn")
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn", "sequence": 2, "type": "tool_result", "name": "read_file",
                                   "result": {"ok": True, "complete": False, "next_line": 10, "content": "hidden-private"}})
        self.assertIn("turn", desktop.views["one"].expanded_working)
        self.assertNotIn("hidden-private", desktop.transcript.get("1.0", tk.END))
        desktop._send_key(None)
        self.assertEqual(desktop.message_input.get("1.0", "end-1c"), "New unsent draft")
        desktop._newline_key(None)
        self.assertIn("\n", desktop.message_input.get("1.0", "end-1c"))

    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_selection_reset_confirmation_keeps_original_session(self, _history: MagicMock, tasks: MagicMock) -> None:
        """Cancel keeps selections, and confirmed changes cannot follow later navigation."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        desktop = FluxDesktop(root, MagicMock(), lambda: None, Path.cwd())
        for owner in ("one", "two"):
            desktop._select_conversation({"conversation_id": owner, "model": "alpha"})
            desktop.views[owner].recover([])
        desktop._confirm_selection("one", "model", "beta")
        tasks.reset_mock()
        decision = next(child for child in root.winfo_children() if hasattr(child, "_flux_decision"))._flux_decision
        decision.choose_option()
        tasks.assert_not_called()
        desktop._confirm_selection("one", "model", "beta")
        decision = next(child for child in root.winfo_children() if hasattr(child, "_flux_decision"))._flux_decision
        decision.move(1)
        decision.choose_option()
        desktop.client.update_conversation.return_value = {"conversation_id": "one", "model": "beta"}
        work, complete = tasks.call_args.args
        complete(work())
        desktop.client.update_conversation.assert_called_once_with("one", "model", "beta")
        self.assertEqual(desktop.active_conversation_id, "two")
        self.assertEqual(desktop.model_text.get(), "alpha")

    @patch("harness_prototype.desktop.Thread")
    @patch.object(FluxDesktop, "_run_task")
    @patch.object(FluxDesktop, "_load_history")
    def test_exit_keep_working_and_stop_bind_original_permission(self, _history: MagicMock, tasks: MagicMock, thread: MagicMock) -> None:
        """Keep work leaves permission unanswered; explicit exit denies and cancels the original turn."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        callbacks = MagicMock()
        root.report_callback_exception = callbacks
        self.addCleanup(callbacks.assert_not_called)
        self.addCleanup(destroy_fixture_root, root)
        stop = MagicMock()
        desktop = FluxDesktop(root, MagicMock(), stop, Path.cwd())
        desktop._select_conversation({"conversation_id": "one"})
        desktop.views["one"].recover([])
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn", "sequence": 1, "type": "turn_started"})
        desktop._turn_event("one", {"conversation_id": "one", "turn_id": "turn", "sequence": 2, "type": "permission_request",
                                   "permission_request_id": "permission", "choices": ["allow_once", "deny"]})
        desktop.close()
        decision = next(child for child in root.winfo_children() if hasattr(child, "_flux_decision"))._flux_decision
        decision.choose_option()
        self.assertFalse(desktop._closing)
        desktop.client.resolve_permission.assert_not_called()
        self.assertIn("permission", desktop._permissions)
        desktop.close()
        decision = next(child for child in root.winfo_children() if hasattr(child, "_flux_decision"))._flux_decision
        decision.move(1)
        decision.choose_option()
        tasks.call_args.args[0]()
        desktop.client.resolve_permission.assert_called_once_with("permission", "deny")
        thread.call_args.kwargs["target"]()
        desktop.client.cancel_turn.assert_called_once_with("one", "turn")
        stop.assert_called_once_with()
        self.assertTrue(desktop._backend_stopped.is_set())

    @patch.object(FluxDesktop, "_run_task")
    def test_settings_save_and_revoke_use_captured_backend_values(self, tasks: MagicMock) -> None:
        """Persist preferences and revoke only the selected durable grant through the API."""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Native Tk display is unavailable.")
        root.withdraw()
        self.addCleanup(destroy_fixture_root, root)
        client = MagicMock()
        desktop = FluxDesktop(root, client, lambda: None, Path.cwd())
        desktop.models = ["test-model"]
        desktop._settings_loaded(({"default_model": "test-model", "backend_host": "http://localhost:11434"},
                                  [{"grant_id": "grant", "kind": "file", "target": "sample.txt", "workspace": "."}]))
        window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
        widgets = window._flux_body.frame.winfo_children()
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
