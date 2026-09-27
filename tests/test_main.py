"""Terminal workflow and failed-turn recovery checks."""

from pathlib import Path
import tomllib
import unittest
from unittest.mock import Mock, patch

from ollama import ChatResponse

from harness_prototype.agent_loop import ContextBudgetError, new_conversation
from harness_prototype.access import AccessChoice
from harness_prototype.approval import ApprovalRequest, ToolCategory
from harness_prototype.main import (
    APP_DESCRIPTION,
    APP_DISPLAY_NAME,
    MODEL_DESCRIPTIONS,
    TerminalUI,
    main,
    recover_turn,
    request_directory_access,
    request_tool_approval,
    select_model,
)


class MainTests(unittest.TestCase):
    """Cover terminal commands, failure reporting, and conversation recovery."""

    def test_application_identity_is_canonical(self) -> None:
        """Keep the product name and short description stable for later interfaces."""
        self.assertEqual(APP_DISPLAY_NAME, "FLUX")
        self.assertEqual(
            APP_DESCRIPTION,
            "A local-first software engineering agent powered by Ollama.",
        )

    def test_header_uses_flux_identity(self) -> None:
        """Present FLUX instead of a legacy or generic name in the session header."""
        ui = TerminalUI()
        with patch("builtins.print") as output:
            ui.show_header("test-model", "http://localhost:11434", Path("workspace"))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("FLUX", rendered)
        self.assertNotIn("CODING AGENT", rendered)

    def test_package_metadata_uses_flux_identity_and_compatibility_name(self) -> None:
        """Brand package metadata as FLUX without renaming the installed package."""
        project_file = Path(__file__).parents[1] / "pyproject.toml"
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["name"], "harness-prototype")
        self.assertEqual(
            project["description"],
            f"{APP_DISPLAY_NAME} — {APP_DESCRIPTION[0].lower()}{APP_DESCRIPTION[1:-1]}",
        )

    def test_select_model_uses_arrow_keys_and_space(self) -> None:
        """Move between configured models and confirm the selection with Space."""
        models = ("slow", "fast")
        ui = TerminalUI()
        with patch.object(ui, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ):
            self.assertEqual(select_model(models, ui), "fast")
        with patch.object(ui, "_read_key", return_value="confirm"), patch(
            "builtins.print"
        ) as output:
            self.assertEqual(select_model(models, ui), "slow")
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertNotIn("(default)", rendered)

    def test_qwen_vision_model_has_capability_description(self) -> None:
        """Describe the vision model's multimodal and agentic strengths."""
        description = MODEL_DESCRIPTIONS["qwen3-vl:8b"]
        self.assertIn("vision", description)
        self.assertIn("images", description)
        self.assertIn("tool use", description)

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

    def test_tool_activity_is_human_readable_and_terminal_safe(self) -> None:
        """Render useful activity dialogue without allowing terminal control codes."""
        ui = TerminalUI()
        with patch("builtins.print") as output:
            ui.show_tool_call("Reading AGENTS.md\033[2J\nnext")
        rendered = output.call_args.args[0]
        self.assertIn("Working    Reading AGENTS.md", rendered)
        self.assertNotIn("\033[2J", rendered)
        self.assertNotIn("\n", rendered)

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
            self.assertEqual(select_model(("first", "second"), ui), "first")

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

    def test_test_approval_shows_scope_without_configured_command(self) -> None:
        """Show bounded test selectors and timeout while keeping command configuration hidden."""
        request = ApprovalRequest(
            "run_tests",
            {"targets": ["tests.test_execution.TestExecutionTests.test_launch_error"]},
            ToolCategory.EXECUTION,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("execution", rendered)
        self.assertIn("1 targeted test selector", rendered)
        self.assertIn("tests.test_execution", rendered)
        self.assertIn("Timeout", rendered)
        self.assertNotIn("uv run", rendered)
        self.assertIn("Allowed 1 test target", rendered)

    def test_documentation_approval_hides_url_payload_and_fails_closed(self) -> None:
        """Show only the network host and deny an interrupted one-time request."""
        request = ApprovalRequest(
            "fetch_documentation",
            {"url": "https://docs.example.com/reference?token=secret"},
            ToolCategory.NETWORK,
        )
        with patch.object(
            TerminalUI, "_read_key", side_effect=KeyboardInterrupt,
        ), patch("builtins.print") as output:
            self.assertFalse(request_tool_approval(request))
        rendered = " ".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertIn("network", rendered)
        self.assertIn("docs.example.com", rendered)
        self.assertIn("Timeout", rendered)
        self.assertNotIn("token", rendered)
        self.assertNotIn("secret", rendered)
        self.assertIn("Denied one documentation request", rendered)

    def test_documentation_session_grant_is_reused_for_later_hosts(self) -> None:
        """Offer clear session access and reuse it for later documentation requests."""
        ui = TerminalUI()
        first = ApprovalRequest(
            "fetch_documentation",
            {"url": "https://docs.python.org/3/"},
            ToolCategory.NETWORK,
        )
        with patch.object(
            ui, "_read_key", side_effect=["down", "confirm"],
        ), patch("builtins.print") as output:
            decision = ui.request_tool_approval(first)
        self.assertTrue(decision)
        self.assertEqual(decision.source, "user_session_grant")
        rendered = " ".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertIn("Allow documentation access for this session", rendered)
        self.assertIn("Allowed documentation access for this session", rendered)

        later = ApprovalRequest(
            "fetch_documentation",
            {"url": "https://developer.mozilla.org/en-US/"},
            ToolCategory.NETWORK,
        )
        with patch.object(
            ui, "_read_key", side_effect=AssertionError("unexpected prompt"),
        ), patch("builtins.print") as output:
            reused = ui.request_tool_approval(later)
        self.assertTrue(reused)
        self.assertEqual(reused.source, "session_documentation_grant")
        rendered = " ".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertIn("Used existing documentation access for this session", rendered)

    def test_test_approval_interrupt_denies_and_leaves_receipt(self) -> None:
        """Fail closed when targeted test approval is interrupted."""
        request = ApprovalRequest("run_tests", {}, ToolCategory.EXECUTION)
        with patch.object(
            TerminalUI, "_read_key", side_effect=KeyboardInterrupt
        ), patch("builtins.print") as output:
            self.assertFalse(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Denied the full test suite", rendered)

    def test_check_approval_shows_only_configured_identity_and_limits(self) -> None:
        """Show the check and target without exposing its configured command."""
        request = ApprovalRequest(
            "run_checks", {"name": "lint", "target": "source"}, ToolCategory.EXECUTION,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("lint", rendered)
        self.assertIn("source", rendered)
        self.assertIn("Timeout", rendered)
        self.assertNotIn("ruff", rendered)
        self.assertIn("Allowed check lint target source", rendered)

    def test_command_approval_summarizes_scope_without_dumping_arguments(self) -> None:
        """Show command identity and bounds while keeping argument payload concise."""
        request = ApprovalRequest(
            "run_command",
            {"command": ["git", "show", "secret-looking-revision"]},
            ToolCategory.EXECUTION,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(request))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("git", rendered)
        self.assertIn("Arguments", rendered)
        self.assertIn("Directory", rendered)
        self.assertIn("Timeout", rendered)
        self.assertNotIn("secret-looking-revision", rendered)
        self.assertIn("Allowed command git in the active workspace", rendered)

    def test_dependency_approval_shows_project_scope_and_timeout(self) -> None:
        """Summarize dependency changes without claiming a manager before detection."""
        request = ApprovalRequest(
            "install_dependencies", {"path": "client"}, ToolCategory.EXECUTION,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(request))
        rendered = " ".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertIn("client", rendered)
        self.assertIn("Directory", rendered)
        self.assertIn("Timeout", rendered)
        self.assertIn("Allowed dependency installation for client", rendered)

    def test_service_approvals_show_identity_without_commands_or_handles(self) -> None:
        """Summarize service lifecycle operations without exposing configured payloads."""
        start = ApprovalRequest("start_service", {"name": "web"}, ToolCategory.EXECUTION)
        stop = ApprovalRequest(
            "stop_service", {"handle": "secret-handle"}, ToolCategory.EXECUTION,
        )
        with patch.object(
            TerminalUI, "_read_key", return_value="confirm"
        ), patch("builtins.print") as output:
            self.assertTrue(request_tool_approval(start))
            self.assertTrue(request_tool_approval(stop))
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("service start for web", rendered)
        self.assertIn("FLUX-managed service", rendered)
        self.assertNotIn("secret-handle", rendered)

    def test_command_session_grant_is_reused_for_the_same_executable(self) -> None:
        """Reuse an executable grant while continuing to prompt for other commands."""
        ui = TerminalUI()
        git_request = ApprovalRequest(
            "run_command", {"command": ["git", "status"]}, ToolCategory.EXECUTION,
        )
        with patch.object(ui, "_read_key", side_effect=["down", "confirm"]), patch(
            "builtins.print"
        ) as output:
            decision = ui.request_tool_approval(git_request)
        self.assertTrue(decision)
        self.assertEqual(decision.source, "user_session_grant")
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Allow all git commands for this session", rendered)
        self.assertIn("Allowed all git commands for this session", rendered)

        with patch.object(
            ui, "_read_key", side_effect=AssertionError("unexpected prompt")
        ), patch("builtins.print") as output:
            reused = ui.request_tool_approval(ApprovalRequest(
                "run_command", {"command": ["GIT", "diff"]}, ToolCategory.EXECUTION,
            ))
        self.assertTrue(reused)
        self.assertEqual(reused.source, "session_command_grant")
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Used existing command access for GIT", rendered)

        other_request = ApprovalRequest(
            "run_command", {"command": ["python", "--version"]},
            ToolCategory.EXECUTION,
        )
        with patch.object(ui, "_read_key", side_effect=["down", "down", "confirm"]), patch(
            "builtins.print"
        ):
            self.assertFalse(ui.request_tool_approval(other_request))

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
        with patch(
            "harness_prototype.main.sys.stdin", isatty=Mock(return_value=False)
        ), patch(
            "harness_prototype.main.sys.stdout", isatty=Mock(return_value=False)
        ), patch.object(
            TerminalUI, "_read_key", side_effect=["down", "confirm"]
        ), patch("builtins.print") as output:
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

    def test_session_exit_cleans_up_managed_services(self) -> None:
        """Stop every harness-launched service when the prompt loop exits."""
        with patch("builtins.input", return_value="quit"), patch(
            "builtins.print"
        ), patch("harness_prototype.main.select_model", return_value="test-model"), patch(
            "harness_prototype.main.Client"
        ), patch("harness_prototype.main.stop_all_services") as cleanup:
            main()
        cleanup.assert_called_once_with()

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
