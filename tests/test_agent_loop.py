"""Deterministic checks for context limits and model/tool exchanges."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ollama import ChatResponse

from harness_prototype.access import AccessChoice, WorkspaceAccess
from harness_prototype.approval import ApprovalPolicy, ToolCategory
from harness_prototype.agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    check_context_budget,
    new_conversation,
    run_turn,
)
from harness_prototype.interaction_log import TurnLog


class AgentLoopTests(unittest.TestCase):
    """Cover successful, exhausted, interrupted, and empty model turns."""

    def test_system_prompt_explains_structured_file_changes(self) -> None:
        """Tell the model how to select safe structured file actions."""
        system_prompt = new_conversation()[0]["content"]
        self.assertIn("never redirect an outside request", system_prompt)
        self.assertIn("structured create, overwrite, append, and replace", system_prompt)
        self.assertIn("pass its revision as expected_revision", system_prompt)
        self.assertIn("Never repeat a failed write unchanged", system_prompt)
        self.assertIn("pass only test selectors", system_prompt)
        self.assertIn("status=failed", system_prompt)

    def test_four_tool_rounds_can_finish(self) -> None:
        """Allow pagination with changed arguments before the final answer."""
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
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(execute.call_count, 4)
        self.assertEqual(outcome, "success")
        self.assertEqual(messages[-1]["content"], "Finished both pages.")

    def test_third_identical_call_stops_without_execution(self) -> None:
        """Stop an identical operation after two executions and retain its results."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "list_files", "arguments": {"path": "."}}}
        ]})
        client = Mock()
        client.chat.side_effect = [[response], [response], [response]]
        messages = new_conversation() + [{"role": "user", "content": "list files"}]
        turn_log = TurnLog("session", "list files")
        with patch("harness_prototype.agent_loop.IDENTICAL_TOOL_CALL_LIMIT", 2), patch(
            "harness_prototype.agent_loop.execute_tool",
            return_value={"ok": True, "complete": True, "truncated": False, "error": None},
        ) as execute:
            outcome = run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
                turn_log=turn_log,
            )
        self.assertEqual(outcome, "tool_loop_limit")
        self.assertEqual(execute.call_count, 2)
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        self.assertEqual(results[-1]["status"], "tool_loop_stopped")
        stop = next(event for event in turn_log.events if event["type"] == "tool_loop_stop")
        self.assertEqual(stop["reason"], "identical_call_limit")
        self.assertIn("Completed results are retained", messages[-1]["content"])

    def test_changed_arguments_allow_corrected_retry(self) -> None:
        """Execute a corrected call and reset the failure streak after it succeeds."""
        responses = [
            ChatResponse(message={"role": "assistant", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "missing.txt"}}}
            ]}),
            ChatResponse(message={"role": "assistant", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "found.txt"}}}
            ]}),
            ChatResponse(message={"role": "assistant", "content": "Recovered."}),
        ]
        client = Mock()
        client.chat.side_effect = [[response] for response in responses]
        messages = new_conversation() + [{"role": "user", "content": "read a file"}]
        with patch(
            "harness_prototype.agent_loop.execute_tool",
            side_effect=[
                {"ok": False, "complete": False, "truncated": False, "error": "missing"},
                {"ok": True, "complete": True, "truncated": False, "error": None},
            ],
        ) as execute:
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(outcome, "success")
        self.assertEqual(execute.call_count, 2)

    def test_consecutive_failures_stop_remaining_calls(self) -> None:
        """Stop after three failures and mark later calls in the batch unexecuted."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": f"missing-{index}.txt"}}}
            for index in range(4)
        ]})
        client = Mock()
        client.chat.return_value = [response]
        messages = new_conversation() + [{"role": "user", "content": "read files"}]
        turn_log = TurnLog("session", "read files")
        failure = {"ok": False, "complete": False, "truncated": False, "error": "missing"}
        with patch("harness_prototype.agent_loop.CONSECUTIVE_TOOL_FAILURE_LIMIT", 3), patch(
            "harness_prototype.agent_loop.execute_tool",
            return_value=failure,
        ) as execute:
            outcome = run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
                turn_log=turn_log,
            )
        self.assertEqual(outcome, "tool_loop_limit")
        self.assertEqual(execute.call_count, 3)
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        self.assertEqual(results[-1]["status"], "not_executed")
        self.assertIn("guidance", results[-2]["loop_stop"])
        stop = next(event for event in turn_log.events if event["type"] == "tool_loop_stop")
        self.assertEqual(stop["reason"], "consecutive_failure_limit")

    def test_round_limit_keeps_results_and_allows_continuation(self) -> None:
        """Retain complete tool history when a turn reaches its round limit."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        client = Mock()
        client.chat.side_effect = [[response], [response]]
        messages = new_conversation()
        with patch("harness_prototype.agent_loop.MAX_TOOL_ROUNDS", 2), patch(
            "harness_prototype.agent_loop.IDENTICAL_TOOL_CALL_LIMIT",
            2,
        ), patch(
            "harness_prototype.agent_loop.execute_tool", return_value={"ok": True}
        ):
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(sum(message["role"] == "tool" for message in messages), 2)
        self.assertEqual(outcome, "tool_round_limit")
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
                with patch("harness_prototype.agent_loop.IDENTICAL_TOOL_CALL_LIMIT", 3), patch(
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

    def test_policy_denial_is_logged_and_prevents_execution(self) -> None:
        """Log the exact denied operation and return it without side effects."""
        arguments = {"path": "."}
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "list_files", "arguments": arguments}}
        ]})
        client = Mock()
        client.chat.side_effect = [
            [response],
            [ChatResponse(message={"role": "assistant", "content": "Operation denied."})],
        ]
        messages = new_conversation() + [{"role": "user", "content": "list files"}]
        turn_log = TurnLog("session", "list files")
        with patch.dict(
            "harness_prototype.agent_loop.APPROVAL_POLICIES",
            {ToolCategory.READ_ONLY: ApprovalPolicy.DENY},
        ), patch("harness_prototype.agent_loop.execute_tool") as execute:
            run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
                turn_log=turn_log,
            )
        execute.assert_not_called()
        result = json.loads(messages[-2]["content"])
        self.assertEqual(result["status"], "approval_denied")
        approval_event = next(event for event in turn_log.events if event["type"] == "tool_approval")
        self.assertEqual(approval_event["decision"], "denied")
        self.assertEqual(approval_event["operation"], {"tool_name": "list_files", "arguments": arguments})

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

    def test_log_preserves_tool_order_and_interrupted_outcomes(self) -> None:
        """Record calls and paired results in order, including skipped calls."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}},
            {"function": {"name": "read_file", "arguments": {"path": "x"}}},
        ]})
        client = Mock()
        client.chat.return_value = [response]
        turn_log = TurnLog("session", "inspect")
        with patch("harness_prototype.agent_loop.execute_tool", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_turn(
                    client,
                    "test",
                    new_conversation() + [{"role": "user", "content": "inspect"}],
                    lambda text: None,
                    lambda name: None,
                    turn_log=turn_log,
                )
        event_types = [event["type"] for event in turn_log.events]
        self.assertEqual(event_types, [
            "model_request", "model_response", "tool_call", "tool_approval", "tool_result",
            "tool_call", "tool_result",
        ])
        results = [event["result"] for event in turn_log.events if event["type"] == "tool_result"]
        self.assertEqual([result["status"] for result in results], ["outcome_unknown", "not_executed"])


if __name__ == "__main__":
    unittest.main()
