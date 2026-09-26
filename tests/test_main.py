"""Terminal workflow and failed-turn recovery checks."""

import unittest
from unittest.mock import Mock, patch

from ollama import ChatResponse

from harness_prototype.agent_loop import ContextBudgetError, new_conversation
from harness_prototype.access import AccessChoice
from harness_prototype.main import main, recover_turn, request_directory_access


class MainTests(unittest.TestCase):
    """Cover terminal commands, failure reporting, and conversation recovery."""

    def test_failure_without_tools_discards_only_new_turn(self) -> None:
        """Remove an uncompleted user turn while retaining earlier history."""
        messages = new_conversation()
        before = list(messages)
        messages.append({"role": "user", "content": "hello"})
        recover_turn(messages, len(before))
        self.assertEqual(messages, before)

    def test_permission_prompt_retries_invalid_input(self) -> None:
        """Render the choices again until the user supplies a valid number."""
        with patch("builtins.input", side_effect=["no", "2"]), patch("builtins.print") as output:
            choice = request_directory_access(Path("outside"))
        self.assertEqual(choice, AccessChoice.GRANT_ALL)
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Please enter 1, 2, or 3", rendered)

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
        ), patch("harness_prototype.main.Client"), patch(
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
        ), patch("harness_prototype.main.Client", return_value=client):
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
        ) as output, patch("harness_prototype.main.Client", return_value=client), patch(
            "harness_prototype.agent_loop.execute_tool", return_value={"ok": True}
        ):
            main()
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("Model response failed", rendered)
        self.assertIn("no text or tool calls", rendered)


if __name__ == "__main__":
    unittest.main()
