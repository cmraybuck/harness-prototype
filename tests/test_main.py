"""Terminal workflow and failed-turn recovery checks."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ollama import ChatResponse

from harness_prototype.agent_loop import ContextBudgetError, new_conversation
from harness_prototype.access import AccessChoice
from harness_prototype.approval import ApprovalRequest, ToolCategory
from harness_prototype.main import (
    TerminalUI,
    main,
    recover_turn,
    request_directory_access,
    request_tool_approval,
    select_model,
)


class MainTests(unittest.TestCase):
    """Cover terminal commands, failure reporting, and conversation recovery."""

    def test_select_model_uses_arrow_keys_and_space(self) -> None:
        """Move between configured models and confirm the selection with Space."""
        models = ("slow", "fast")
        ui = TerminalUI()
        with patch.object(ui, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ):
            self.assertEqual(select_model(models, "slow", ui), "fast")
        with patch.object(ui, "_read_key", return_value="confirm"), patch("builtins.print"):
            self.assertEqual(select_model(models, "fast", ui), "fast")

    def test_streamed_response_cannot_overwrite_prior_output(self) -> None:
        """Convert carriage returns from model output into ordinary newlines."""
        ui = TerminalUI()
        with patch("builtins.print") as output:
            ui.write_response("first\rsecond")
        rendered = "".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertNotIn("\r", rendered)
        self.assertIn("first\nsecond", rendered)

    def test_clear_screen_erases_display_and_scrollback_for_a_terminal(self) -> None:
        """Clear both prior output and scrollback only for interactive output."""
        ui = TerminalUI()
        with patch("harness_prototype.main.sys.stdout", isatty=Mock(return_value=True)), patch(
            "builtins.print"
        ) as output:
            ui.clear_screen()
        self.assertEqual(output.call_args.args[0], "\033[2J\033[3J\033[H")

        with patch("harness_prototype.main.sys.stdout", isatty=Mock(return_value=False)), patch(
            "builtins.print"
        ) as output:
            ui.clear_screen()
        output.assert_not_called()

    def test_dialogue_ignores_non_navigation_keys(self) -> None:
        """Keep the current selection when a dialogue receives an unrelated key."""
        ui = TerminalUI()
        with patch.object(ui, "_read_key", side_effect=["other", "confirm"]), patch(
            "builtins.print"
        ):
            self.assertEqual(select_model(("first", "second"), "first", ui), "first")

    def test_enter_confirms_the_selected_dialogue_option(self) -> None:
        """Treat Enter as an equivalent confirmation key to Space."""
        ui = TerminalUI()
        with patch("harness_prototype.main.os.name", "nt"), patch(
            "msvcrt.getwch", return_value="\r"
        ):
            self.assertEqual(ui._read_key(), "confirm")

    def test_dialogue_uses_cyan_and_green_application_palette(self) -> None:
        """Render dialogue framing in cyan and its selection in green."""
        ui = TerminalUI()
        ui.use_color = True
        with patch.object(ui, "_read_key", return_value="confirm"), patch(
            "builtins.print"
        ) as output:
            self.assertEqual(ui.choose_option("Permission", [], ["Allow"], transient=False), 0)
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("\033[1;36m  PERMISSION", rendered)
        self.assertIn("\033[1;32m  > Allow", rendered)

    def test_failure_without_tools_discards_only_new_turn(self) -> None:
        """Remove an uncompleted user turn while retaining earlier history."""
        messages = new_conversation()
        before = list(messages)
        messages.append({"role": "user", "content": "hello"})
        recover_turn(messages, len(before))
        self.assertEqual(messages, before)

    def test_directory_permission_uses_arrow_keys_and_space(self) -> None:
        """Navigate from directory access to full access and confirm it."""
        with patch.object(
            TerminalUI, "_read_key", side_effect=["down", "confirm"]
        ), patch("builtins.print") as output:
            choice = request_directory_access(Path("outside"))
        self.assertEqual(choice, AccessChoice.GRANT_ALL)
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Use Up/Down to select, then Space or Enter to confirm", rendered)
        self.assertIn("Allowed access to all directories", rendered)

    def test_tool_approval_prompt_hides_payload_and_leaves_receipt(self) -> None:
        """Summarize a write without retaining its content in the conversation."""
        request = ApprovalRequest(
            "apply_file_changes",
            {
                "base_path": "project",
                "changes": [{"action": "create", "path": "notes.txt", "content": "secret"}],
            },
            ToolCategory.WRITE,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("write", rendered)
        self.assertIn("create: notes.txt", rendered)
        self.assertNotIn("secret", rendered)
        self.assertIn("Allowed 1 file change", rendered)

    def test_interactive_permission_uses_and_restores_alternate_screen(self) -> None:
        """Keep an interactive permission screen out of conversation scrollback."""
        ui = TerminalUI()
        request = ApprovalRequest("deploy", {}, ToolCategory.DEPLOYMENT)
        with patch("harness_prototype.main.sys.stdin", isatty=Mock(return_value=True)), patch(
            "harness_prototype.main.sys.stdout", isatty=Mock(return_value=True)
        ), patch.object(ui, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ) as output:
            self.assertFalse(ui.request_tool_approval(request))
        rendered = "".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("\033[?1049h", rendered)
        self.assertIn("\033[?1049l", rendered)
        self.assertIn("Denied deploy", rendered)

    def test_tool_approval_sanitizes_untrusted_display_fields(self) -> None:
        """Prevent paths from injecting controls or expanding the review view."""
        unsafe_path = "notes\033[2J\n" + ("x" * 300)
        request = ApprovalRequest(
            "apply_file_changes",
            {"changes": [{"action": "create", "path": unsafe_path, "content": "safe"}]},
            ToolCategory.WRITE,
        )
        with patch.object(TerminalUI, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ) as output:
            self.assertFalse(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertNotIn("\033[2J", rendered)
        self.assertNotIn("x" * 200, rendered)

    def test_file_edit_grant_is_reused_for_the_same_file(self) -> None:
        """Skip later prompts after all edits to one exact file are allowed."""
        ui = TerminalUI()
        request = ApprovalRequest(
            "apply_file_changes",
            {
                "base_path": ".",
                "changes": [{"action": "create", "path": "notes.txt", "content": "first"}],
            },
            ToolCategory.WRITE,
        )
        with patch.object(ui, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ):
            self.assertTrue(ui.request_tool_approval(request))
        with patch.object(ui, "_read_key", side_effect=AssertionError("unexpected prompt")), patch(
            "builtins.print"
        ) as output:
            self.assertTrue(ui.request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Used existing edit access", rendered)

    def test_permission_receipt_matches_subdued_tool_activity_layout(self) -> None:
        """Indent permission receipts and render them with subdued activity styling."""
        ui = TerminalUI()
        ui.use_color = True
        with patch("builtins.print") as output:
            ui.show_permission_receipt("Allowed one operation.")
        rendered = output.call_args.args[0]
        self.assertIn("\033[2;36m  Permission Allowed one operation.", rendered)

    def test_context_stop_can_be_cleared(self) -> None:
        """Allow explicit reset after a budget stop and submit a fresh prompt."""
        snapshots = []

        def turn(**kwargs) -> None:
            """Capture history and simulate a budget stop on the first request."""
            snapshots.append(list(kwargs["messages"]))
            if len(snapshots) == 1:
                raise ContextBudgetError("clear required")

        with patch("builtins.input", side_effect=["large", "clear", "small", "quit"]), patch(
            "builtins.print"
        ), patch("harness_prototype.main.select_model", return_value="test-model"), patch(
            "harness_prototype.main.Client"
        ), patch(
            "harness_prototype.main.run_turn", side_effect=turn
        ):
            main()
        self.assertEqual(len(snapshots[1]), 2)
        self.assertEqual(snapshots[1][-1]["content"], "small")

    def test_cancelled_stream_recovers_for_next_prompt(self) -> None:
        """Discard incomplete streamed text and accept another turn."""
        client = Mock()

        def interrupted_stream():
            """Yield partial text before simulating cancellation."""
            yield ChatResponse(message={"role": "assistant", "content": "partial"})
            raise KeyboardInterrupt

        histories = []

        def request(**kwargs):
            """Record model requests and supply interrupted then final replies."""
            histories.append(list(kwargs["messages"]))
            if len(histories) == 1:
                return interrupted_stream()
            return [ChatResponse(message={"role": "assistant", "content": "Done"})]

        client.chat.side_effect = request
        with patch("builtins.input", side_effect=["first", "second", "quit"]), patch(
            "builtins.print"
        ), patch("harness_prototype.main.select_model", return_value="test-model"), patch(
            "harness_prototype.main.Client", return_value=client
        ):
            main()
        self.assertEqual(len(histories[1]), 2)
        self.assertEqual(histories[1][-1]["content"], "second")

    def test_reports_empty_model_response(self) -> None:
        """Report an empty post-tool completion instead of silently returning."""
        client = Mock()
        tool_request = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        empty_response = ChatResponse(message={"role": "assistant"})
        client.chat.side_effect = [[tool_request], [empty_response], [empty_response]]
        with patch("builtins.input", side_effect=["find a file", "quit"]), patch(
            "builtins.print"
        ) as output, patch("harness_prototype.main.select_model", return_value="test-model"), patch(
            "harness_prototype.main.Client", return_value=client
        ), patch(
            "harness_prototype.agent_loop.execute_tool", return_value={"ok": True}
        ):
            main()
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Model response failed", rendered)
        self.assertIn("no text or tool calls", rendered)


if __name__ == "__main__":
    unittest.main()
