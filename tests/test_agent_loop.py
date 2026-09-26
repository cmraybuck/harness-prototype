"""Deterministic checks for context limits and model/tool exchanges."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ollama import ChatResponse

from harness_prototype.access import AccessChoice, WorkspaceAccess
from harness_prototype.agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    check_context_budget,
    new_conversation,
    run_turn,
)


class AgentLoopTests(unittest.TestCase):
    """Cover successful, exhausted, interrupted, and empty model turns."""

    def test_four_tool_rounds_can_finish(self) -> None:
        """Allow discovery, search, and two read pages before the final answer."""
        calls = [
            ("get_location", {"location": "desktop"}),
            ("find_files", {"pattern": "x"}),
            ("read_file", {"path": "x"}),
            ("read_file", {"path": "x", "start_line": 101}),
        ]
        responses = [
            ChatResponse(message={"role": "assistant", "tool_calls": [
                {"function": {"name": name, "arguments": arguments}}
            ]})
            for name, arguments in calls
        ]
        responses.append(ChatResponse(message={"role": "assistant", "content": "Finished both pages."}))
        messages = new_conversation()
        with patch("harness_prototype.agent_loop.execute_tool", return_value={"ok": True}) as execute:
            client = Mock()
            client.chat.side_effect = [[response] for response in responses]
            run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(execute.call_count, 4)
        self.assertEqual(messages[-1]["content"], "Finished both pages.")

    def test_round_limit_keeps_results_and_allows_continuation(self) -> None:
        """Retain complete tool history when a turn reaches its round limit."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        client = Mock()
        client.chat.side_effect = [[response], [response]]
        messages = new_conversation()
        with patch("harness_prototype.agent_loop.MAX_TOOL_ROUNDS", 2), patch(
            "harness_prototype.agent_loop.execute_tool", return_value={"ok": True}
        ):
            run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(sum(message["role"] == "tool" for message in messages), 2)
        self.assertIn("retained", messages[-1]["content"])
        messages.append({"role": "user", "content": "continue"})
        client.chat.side_effect = [[ChatResponse(message={"role": "assistant", "content": "Done."})]]
        run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(messages[-1]["content"], "Done.")

    def test_context_guard_preserves_history_and_prevents_request(self) -> None:
        """Stop an oversized request locally without mutating its history."""
        with patch("harness_prototype.agent_loop.CONTEXT_LENGTH", 4096):
            check_context_budget(new_conversation())
            messages = new_conversation() + [{"role": "user", "content": "x" * 15000}]
            before = list(messages)
            client = Mock()
            with self.assertRaises(ContextBudgetError):
                run_turn(client, "test", messages, lambda text: None, lambda name: None)
            client.chat.assert_not_called()
            self.assertEqual(messages, before)

    def test_interrupted_batch_records_all_outcomes(self) -> None:
        """Pair every requested tool after execution fails or is cancelled."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
            for _ in range(3)
        ]})
        for failure in (RuntimeError("failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                messages = new_conversation() + [{"role": "user", "content": "inspect"}]
                client = Mock()
                client.chat.return_value = [response]
                with patch(
                    "harness_prototype.agent_loop.execute_tool",
                    side_effect=[{"ok": True}, failure],
                ) as execute:
                    with self.assertRaises(type(failure)):
                        run_turn(client, "test", messages, lambda text: None, lambda name: None)
                self.assertEqual(execute.call_count, 2)
                outcomes = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
                self.assertTrue(outcomes[0]["ok"])
                self.assertEqual(outcomes[1]["status"], "outcome_unknown")
                self.assertEqual(outcomes[2]["status"], "not_executed")

    def test_model_failure_after_tool_keeps_result(self) -> None:
        """Preserve a completed exchange if the next model request fails."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        client = Mock()
        client.chat.side_effect = [[response], RuntimeError("server down")]
        messages = new_conversation() + [{"role": "user", "content": "inspect"}]
        with patch("harness_prototype.agent_loop.execute_tool", return_value={"ok": True}):
            with self.assertRaises(RuntimeError):
                run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(messages[-1]["role"], "tool")

    def test_denied_access_returns_a_tool_result_without_execution(self) -> None:
        """Return a refusal to the model without invoking the filesystem tool."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "list_files", "arguments": {"path": "../outside"}}}
        ]})
        client = Mock()
        client.chat.side_effect = [
            [response],
            [ChatResponse(message={"role": "assistant", "content": "Access was denied."})],
        ]
        messages = new_conversation() + [{"role": "user", "content": "look outside"}]
        access = WorkspaceAccess(Path.cwd())
        with patch("harness_prototype.agent_loop.execute_tool") as execute:
            run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
                access,
                lambda path: AccessChoice.DENY,
            )
        execute.assert_not_called()
        outcome = json.loads(messages[-2]["content"])
        self.assertEqual(outcome["status"], "access_denied")

    def test_transient_empty_response_is_retried(self) -> None:
        """Retry one empty completion and retain the eventual answer."""
        messages = new_conversation() + [{"role": "user", "content": "find a file"}]
        client = Mock()
        client.chat.side_effect = [
            [ChatResponse(message={"role": "assistant"})],
            [ChatResponse(message={"role": "assistant", "content": "Found it."})],
        ]
        run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(client.chat.call_count, 2)
        self.assertEqual(messages[-1]["content"], "Found it.")

    def test_repeated_empty_response_does_not_enter_history(self) -> None:
        """Reject repeated empty output without recording an assistant turn."""
        messages = new_conversation() + [{"role": "user", "content": "find a file"}]
        client = Mock()
        empty_response = ChatResponse(message={"role": "assistant"})
        client.chat.side_effect = [[empty_response], [empty_response]]
        with self.assertRaises(EmptyModelResponseError):
            run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(messages[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
