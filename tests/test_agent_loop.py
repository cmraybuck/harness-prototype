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
    grounding_instruction,
    inspection_requirements,
    inspection_retry_instruction,
    new_conversation,
    request_context_length,
    requires_repository_inspection,
    remember_role_candidates,
    requested_answer_checklist,
    requested_evidence_targets,
    run_turn,
    supplementary_citation_reads,
    validate_grounded_answer,
    validate_requested_answer_coverage,
)
from harness_prototype.interaction_log import TurnLog


class AgentLoopTests(unittest.TestCase):
    """Cover successful, exhausted, interrupted, and empty model turns."""

    def test_system_prompt_explains_structured_file_changes(self) -> None:
        """Tell the model how to select safe structured file actions."""
        system_prompt = new_conversation()[0]["content"]
        self.assertIn("never redirect that request into the workspace", system_prompt)
        self.assertIn("structured create, overwrite, append, and replace", system_prompt)
        self.assertIn("pass its revision as expected_revision", system_prompt)
        self.assertIn("Never repeat a failed write unchanged", system_prompt)
        self.assertIn("pass only test selectors", system_prompt)
        self.assertIn("status=failed", system_prompt)
        self.assertIn("Use inspect_file for JSON, TOML, YAML, PDF, DOCX", system_prompt)
        self.assertIn("selected model to support vision", system_prompt)

    def test_explicit_tool_request_bypasses_repository_evidence_gate(self) -> None:
        """Let a named inspection tool answer directly instead of forcing searches."""
        prompt = (
            "Use detect_project_technologies on the current working directory. "
            "Do not run tests or execute project code. Report every detected project, "
            "language, package manager, test framework, and script."
        )

        self.assertFalse(requires_repository_inspection([
            {"role": "user", "content": prompt},
        ]))

    def test_image_inspection_is_forwarded_to_the_model(self) -> None:
        """Attach a validated image path to its tool-result message."""
        tool_call = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {"name": "inspect_file", "arguments": {"path": "sample.png"}}
        }]})
        answer = ChatResponse(message={"role": "assistant", "content": "A red square."})
        client = Mock()
        client.chat.side_effect = [[tool_call], [answer]]
        result = {
            "ok": True,
            "error": None,
            "complete": True,
            "truncated": False,
            "path": "sample.png",
            "image_for_model": True,
        }
        messages = new_conversation() + [{"role": "user", "content": "Inspect sample.png"}]
        with patch("harness_prototype.agent_loop.execute_tool", return_value=result):
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        tool_message = next(message for message in messages if message["role"] == "tool")
        self.assertEqual(outcome, "success")
        self.assertEqual(tool_message["images"], ["sample.png"])

    def test_implementation_question_still_requires_repository_evidence(self) -> None:
        """Retain the evidence gate for questions about a named tool's implementation."""
        self.assertTrue(requires_repository_inspection([{
            "role": "user",
            "content": (
                "Explain the implementation and tests for "
                "detect_project_technologies in this repository."
            ),
        }]))

    def test_system_prompt_requires_evidence_before_diagnosis(self) -> None:
        """Require implementation evidence, causal tracing, and honest confidence."""
        system_prompt = new_conversation()[0]["content"]
        self.assertIn("Search excerpts are navigation aids", system_prompt)
        self.assertIn("read its implementation", system_prompt)
        self.assertIn("inspect focused tests", system_prompt)
        self.assertIn("inspect the relevant git_diff", system_prompt)
        self.assertIn("exact observed expression or branch", system_prompt)
        self.assertIn("explain the causal chain", system_prompt)
        self.assertIn("strongly supported but unverified", system_prompt)
        self.assertIn("do not change files", system_prompt)

    def test_system_prompt_teaches_search_recovery_and_coding_workflow(self) -> None:
        """Give weaker models concrete fallbacks for brittle literal searches."""
        system_prompt = new_conversation()[0]["content"]
        self.assertIn("automatically leads with exact matches", system_prompt)
        self.assertIn("fallback_used and match_counts", system_prompt)
        self.assertIn("suggested_queries anchor", system_prompt)
        self.assertIn("Change the query rather than repeating an empty search", system_prompt)
        self.assertIn("Multiple search_project content matches are normal", system_prompt)
        self.assertIn("locate its implementation", system_prompt)
        self.assertIn("inspect focused tests and documentation", system_prompt)
        self.assertIn("review every changed file", system_prompt)

    def test_repository_request_retries_an_unsupported_answer_with_tools(self) -> None:
        """Withhold speculation and require an inspection attempt before answering."""
        unsupported = ChatResponse(message={
            "role": "assistant",
            "content": "The threshold is probably internal.",
        })
        search_call = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {
                "name": "search_project",
                "arguments": {"text": "IDENTICAL_TOOL_CALL_LIMIT"},
            },
        }]})
        unsupported_after_search = ChatResponse(message={
            "role": "assistant",
            "content": "The search confirms the implementation.",
        })
        read_call = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {
                "name": "read_file",
                "arguments": {"path": "src/harness_prototype/loop_guard.py"},
            },
        }]})
        supported = ChatResponse(message={
            "role": "assistant",
            "content": (
                "The inspected loop_guard.py function defines the threshold "
                "[src/harness_prototype/loop_guard.py:1]."
            ),
        })
        client = Mock()
        client.chat.side_effect = [
            [unsupported], [search_call], [unsupported_after_search], [read_call], [supported],
        ]
        rendered = []
        messages = new_conversation() + [{
            "role": "user",
            "content": "Identify the exact file and function in this repository.",
        }]

        with patch(
            "harness_prototype.agent_loop.execute_tool",
            side_effect=[
                {
                    "ok": True,
                    "complete": True,
                    "truncated": False,
                    "matches": [{"path": "src/harness_prototype/loop_guard.py"}],
                },
                {
                    "ok": True,
                    "complete": True,
                    "truncated": False,
                    "path": "src/harness_prototype/loop_guard.py",
                    "start_line": 1,
                    "lines_read": 40,
                },
            ],
        ) as execute:
            outcome = run_turn(client, "test", messages, rendered.append, lambda name: None)

        self.assertEqual(outcome, "success")
        self.assertEqual(execute.call_count, 2)
        self.assertNotIn("probably internal", "".join(rendered))
        self.assertNotIn("search confirms", "".join(rendered))
        self.assertIn("loop_guard.py function", "".join(rendered))
        retry_messages = client.chat.call_args_list[1].kwargs["messages"]
        self.assertEqual(retry_messages[-1]["role"], "user")
        self.assertIn("Call search_project", retry_messages[-1]["content"])
        first_request = client.chat.call_args_list[0].kwargs["messages"]
        self.assertIn("Call search_project", first_request[-1]["content"])
        read_retry = client.chat.call_args_list[3].kwargs["messages"][-1]["content"]
        self.assertIn("Call read_file now", read_retry)

    def test_repository_request_fails_closed_after_bounded_unsupported_answers(self) -> None:
        """Return an honest notice when a model repeatedly refuses inspection."""
        unsupported = ChatResponse(message={
            "role": "assistant",
            "content": "It is likely implemented internally.",
        })
        client = Mock()
        client.chat.side_effect = [[unsupported]] * 5
        rendered = []
        messages = new_conversation() + [{
            "role": "user",
            "content": "Identify the exact files, functions, and tests in this repository.",
        }]

        outcome = run_turn(client, "test", messages, rendered.append, lambda name: None)

        self.assertEqual(outcome, "inspection_required")
        self.assertNotIn("likely implemented", "".join(rendered))
        self.assertIn("could not complete the repository inspection", "".join(rendered))

    def test_grounded_answer_requires_read_ranges_and_requested_categories(self) -> None:
        """Accept simple evidence IDs and reject unknown or unread citations."""
        evidence = {
            "C:/project/src/app.py": [(10, 30)],
            "C:/project/tests/test_app.py": [(40, 60)],
        }
        valid = (
            "The implementation handles it [E1:12], and the focused test "
            "covers the behavior [E2:45]."
        )
        self.assertEqual(
            validate_grounded_answer(valid, evidence, {"source", "test"}, 1),
            [],
        )
        errors = validate_grounded_answer(
            "The implementation handles it [E9:12].",
            evidence,
            {"source", "test"},
            1,
        )
        self.assertTrue(any("Unknown evidence citation" in error for error in errors))
        self.assertTrue(any("Missing cited evidence categories" in error for error in errors))

    def test_grounding_instruction_supplies_copyable_evidence_ids(self) -> None:
        """Give a weak model short citation tokens with exact valid examples."""
        instruction = grounding_instruction(
            {
                "C:/project/src/loop_guard.py": [(40, 80)],
                "C:/project/tests/test_loop_guard.py": [(10, 30)],
            },
            {"source", "test"},
            original_prompt=(
                "Explain the configured threshold, identical request criteria, "
                "retained history, final user-visible outcome, and tests."
            ),
        )

        self.assertIn("E1 = C:/project/src/loop_guard.py", instruction)
        self.assertIn("example [E1:40]", instruction)
        self.assertIn("E2 = C:/project/tests/test_loop_guard.py", instruction)
        self.assertIn("parentheses are accepted", instruction)
        self.assertIn("entire range was read", instruction)
        self.assertIn("Required answer checklist", instruction)
        self.assertIn("both tool name and arguments", instruction)
        self.assertIn("What conversation history is retained", instruction)

    def test_requested_answer_coverage_reports_omitted_facets(self) -> None:
        """Require every explicitly requested synthesis facet without more tools."""
        prompt = (
            "Trace configured identical requests through enforcement to the final "
            "user-visible outcome, including retained history and focused tests. "
            "Distinguish confirmed behavior from inference."
        )
        complete = (
            "Confirmed configuration sets the limit. Identical means the tool name "
            "and arguments match. Enforcement returns tool_loop_stopped. Conversation "
            "history is retained. The user-visible notice explains the outcome. The "
            "focused test proves it. Inference: none."
        )

        self.assertEqual(validate_requested_answer_coverage(complete, prompt), [])
        errors = validate_requested_answer_coverage(
            "Configuration sets a threshold and tests cover enforcement.",
            prompt,
        )

        self.assertTrue(any("both tool name and arguments" in error for error in errors))
        self.assertTrue(any("conversation history" in error for error in errors))
        self.assertTrue(any("source-confirmed facts" in error for error in errors))
        self.assertTrue(requested_answer_checklist(prompt))

    def test_grounding_accepts_parentheses_and_fills_unread_citation_ranges(self) -> None:
        """Recover natural citation formatting and batch only missing line windows."""
        evidence = {
            "C:/project/src/loop_guard.py": [(20, 90)],
            "C:/project/tests/test_agent_loop.py": [(190, 270)],
        }
        draft = (
            "Enforcement is confirmed (E1:40-80), while the focused test "
            "is outside the current slice (E2:340)."
        )

        errors = validate_grounded_answer(
            draft,
            evidence,
            {"source", "test"},
            1,
        )
        requests = supplementary_citation_reads(draft, evidence)

        self.assertNotIn("The answer contains no ROLE:line evidence citations.", errors)
        self.assertTrue(any("E2:340" in error for error in errors))
        self.assertEqual(requests, [{
            "path": "C:/project/tests/test_agent_loop.py",
            "start_line": 320,
            "max_lines": 80,
            "roles": [],
        }])

    def test_named_evidence_requires_its_exact_semantic_anchor(self) -> None:
        """Reject a role citation that points only at an unrelated neighboring line."""
        path = "C:/project/src/loop_guard.py"
        evidence = {path: [(20, 90)]}
        named = {
            "IDENTITY": {
                "path": path,
                "description": "tool name and argument signature",
                "spans": [(20, 90)],
                "anchor_lines": [65],
            },
        }

        errors = validate_grounded_answer(
            "Identity is defined here [IDENTITY:20].",
            evidence,
            {"source"},
            1,
            named,
        )

        self.assertTrue(any("Missing exact anchor" in error for error in errors))
        self.assertEqual(
            validate_grounded_answer(
                "Identity is defined here [IDENTITY:65].",
                evidence,
                {"source"},
                1,
                named,
            ),
            [],
        )

    def test_inspection_retry_selects_an_unread_search_match(self) -> None:
        """Advance through relevant search results instead of rereading the first hit."""
        result = {
            "matches": [
                {
                    "path": "C:/project/src/agent_loop.py",
                    "line_number": 100,
                },
                {
                    "path": "C:/project/src/loop_guard.py",
                    "line_number": 40,
                },
            ],
        }

        instruction = inspection_retry_instruction(
            "Trace identical calls through enforcement.",
            {"source"},
            1,
            {"C:/project/src/agent_loop.py"},
            result["matches"],
            {"identical"},
        )

        self.assertIn("C:/project/src/loop_guard.py", instruction)
        self.assertNotIn("C:/project/src/agent_loop.py", instruction)

    def test_user_visible_outcome_does_not_force_cli_evidence(self) -> None:
        """Treat a user-visible notice as behavior, not automatically as CLI wiring."""
        categories, source_reads = inspection_requirements(
            "Trace enforcement through the final user-visible outcome and tests."
        )

        self.assertEqual(categories, {"source", "test"})
        self.assertEqual(source_reads, 2)

    def test_repeated_tool_prompt_gets_named_semantic_evidence_targets(self) -> None:
        """Plan exact searches for each requested anti-loop behavior facet."""
        targets = requested_evidence_targets(
            "Trace configured identical tool operations through enforcement, retained "
            "conversation history, the final user-visible notice, and focused tests."
        )

        self.assertEqual(
            [target["role"] for target in targets],
            ["CONFIG", "IDENTITY", "ENFORCEMENT", "NOTICE", "TURN_HISTORY", "TEST"],
        )
        self.assertEqual(
            next(target for target in targets if target["role"] == "IDENTITY")["query"],
            "signature = json.dumps",
        )

    def test_test_role_prefers_the_definition_over_string_references(self) -> None:
        """Keep planner and documentation mentions from outranking the actual test."""
        candidates = {}
        target = {
            "role": "TEST",
            "description": "focused regression",
            "query": "test_example",
            "file_pattern": "test*.py",
        }
        remember_role_candidates(candidates, target, {
            "matches": [
                {"path": "tests/test_app.py", "line_number": 10, "excerpt": '"test_example"'},
                {"path": "tests/test_app.py", "line_number": 40, "excerpt": "def test_example"},
            ],
        })

        self.assertEqual(candidates["TEST"][0]["line_number"], 40)

    def test_request_context_grows_in_bounded_four_thousand_token_tiers(self) -> None:
        """Keep small research requests below their configured maximum window."""
        with patch("harness_prototype.agent_loop.CONTEXT_LENGTH", 4096):
            self.assertEqual(request_context_length(3000, 32768), 4096)
            self.assertEqual(request_context_length(5000, 32768), 8192)
            self.assertEqual(request_context_length(30000, 32768), 32768)
            self.assertEqual(request_context_length(3000, 16384, 16384), 16384)

    def test_repository_research_batches_cached_evidence_reads(self) -> None:
        """Complete a role-specific trace in three batched model requests."""
        target_searches = [
            ("HARNESS_IDENTICAL_TOOL_CALL_LIMIT", "config.py"),
            ("signature = json.dumps", "loop_guard.py"),
            ("count >= self.identical_call_limit", "loop_guard.py"),
            ("def notice", "loop_guard.py"),
            ("repeated_call_stop = loop_guard.before_call", "agent_loop.py"),
            ("test_third_identical_call_stops_without_execution", "test*.py"),
        ]
        search = ChatResponse(message={"role": "assistant", "tool_calls": [
            {
                "function": {
                    "name": "search_project",
                    "arguments": {"text": text, "path": ".", "file_pattern": pattern},
                },
            }
            for text, pattern in target_searches
        ]})
        paths = [
            "C:/project/src/config.py",
            "C:/project/src/agent_loop.py",
            "C:/project/src/loop_guard.py",
            "C:/project/tests/test_agent_loop.py",
        ]
        batch = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": path}}}
            for path in paths
        ]})
        final = ChatResponse(message={
            "role": "assistant",
            "content": (
                "Confirmed configuration sets the threshold [CONFIG:200]. Identical "
                "means the tool name and arguments match [IDENTITY:50]. Enforcement "
                "uses the stop branch [ENFORCEMENT:67] and returns a user-visible "
                "notice [NOTICE:30]. Conversation history is retained by the turn "
                "flow [TURN_HISTORY:780]. The focused test proves the behavior "
                "[TEST:480]. Inference: none."
            ),
        })
        role_locations = [
            (paths[0], 200),
            (paths[2], 50),
            (paths[2], 67),
            (paths[2], 30),
            (paths[1], 780),
            (paths[3], 480),
        ]
        search_results = [
            {
                "ok": True,
                "complete": True,
                "truncated": False,
                "matches": [{"path": path, "line_number": line}],
            }
            for path, line in role_locations
        ]
        read_results = [
            {
                "ok": True,
                "complete": True,
                "truncated": False,
                "path": path,
                "start_line": line,
                "lines_read": 80,
            }
            for path, line in zip(paths, (180, 760, 10, 460), strict=True)
        ]
        client = Mock()
        client.chat.side_effect = [[search], [batch], [final]]
        messages = new_conversation() + [{
            "role": "user",
            "content": (
                "Trace identical tool-call configuration through enforcement, the "
                "retained conversation history, user-visible outcome, focused files "
                "and functions, and tests in this repository. Distinguish confirmed "
                "facts from inference."
            ),
        }]

        with patch(
            "harness_prototype.agent_loop.execute_tool",
            side_effect=[*search_results, *read_results],
        ) as execute:
            outcome = run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
            )

        self.assertEqual(outcome, "success")
        self.assertEqual(client.chat.call_count, 3)
        self.assertEqual(execute.call_count, 10)
        second_instruction = client.chat.call_args_list[1].kwargs["messages"][-1]["content"]
        self.assertIn("Semantic evidence is missing", second_instruction)
        self.assertEqual(second_instruction.count("read_file("), 4)

    def test_grounding_expands_an_unread_citation_before_retrying(self) -> None:
        """Turn a useful out-of-range citation into one focused evidence read."""
        path = "C:/project/src/loop_guard.py"
        search = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {
                "name": "search_project",
                "arguments": {"text": "identical", "path": ".", "file_pattern": "*.py"},
            },
        }]})
        initial_read = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {"name": "read_file", "arguments": {"path": path}},
        }]})
        incomplete_draft = ChatResponse(message={
            "role": "assistant",
            "content": (
                "The loop_guard.py function contains the enforcement branch outside "
                "the first slice (E1:200)."
            ),
        })
        focused_read = ChatResponse(message={"role": "assistant", "tool_calls": [{
            "function": {
                "name": "read_file",
                "arguments": {"path": path, "start_line": 180, "max_lines": 80},
            },
        }]})
        client = Mock()
        client.chat.side_effect = [
            [search], [initial_read], [incomplete_draft], [focused_read],
        ]
        messages = new_conversation() + [{
            "role": "user",
            "content": "Identify the exact file and function in this repository.",
        }]

        with patch(
            "harness_prototype.agent_loop.execute_tool",
            side_effect=[
                {
                    "ok": True,
                    "complete": True,
                    "truncated": False,
                    "matches": [{"path": path, "line_number": 20}],
                },
                {
                    "ok": True,
                    "complete": True,
                    "truncated": False,
                    "path": path,
                    "start_line": 1,
                    "lines_read": 80,
                },
                {
                    "ok": True,
                    "complete": True,
                    "truncated": False,
                    "path": path,
                    "start_line": 180,
                    "lines_read": 80,
                },
            ],
        ):
            outcome = run_turn(
                client,
                "test",
                messages,
                lambda text: None,
                lambda name: None,
            )

        self.assertEqual(outcome, "success")
        self.assertEqual(client.chat.call_count, 4)
        expansion = client.chat.call_args_list[3].kwargs["messages"][-1]["content"]
        self.assertIn("start_line=180", expansion)
        self.assertIn("focused read", expansion)
        self.assertEqual(messages[-1]["content"], incomplete_draft.message.content)

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

    def test_round_budget_allows_a_final_answer_after_last_tool_round(self) -> None:
        """Reserve a tool-free synthesis request after the execution budget."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        client = Mock()
        final = ChatResponse(message={"role": "assistant", "content": "Finished."})
        client.chat.side_effect = [[response], [response], [final]]
        messages = new_conversation()
        with patch("harness_prototype.agent_loop.MAX_TOOL_ROUNDS", 2), patch(
            "harness_prototype.agent_loop.IDENTICAL_TOOL_CALL_LIMIT",
            2,
        ), patch(
            "harness_prototype.agent_loop.execute_tool", return_value={"ok": True}
        ):
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(sum(message["role"] == "tool" for message in messages), 2)
        self.assertEqual(outcome, "success")
        self.assertEqual(messages[-1]["content"], "Finished.")

    def test_round_budget_rejects_an_additional_tool_request(self) -> None:
        """Pair over-budget calls with explicit non-execution results."""
        response = ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_location", "arguments": {"location": "desktop"}}}
        ]})
        client = Mock()
        client.chat.side_effect = [[response], [response]]
        messages = new_conversation()
        with patch("harness_prototype.agent_loop.MAX_TOOL_ROUNDS", 1), patch(
            "harness_prototype.agent_loop.execute_tool",
            return_value={"ok": True},
        ) as execute:
            outcome = run_turn(client, "test", messages, lambda text: None, lambda name: None)
        self.assertEqual(outcome, "tool_round_limit")
        self.assertEqual(execute.call_count, 1)
        results = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        ]
        self.assertEqual(results[-1]["status"], "tool_round_limit")

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

    def test_context_guard_counts_generated_model_tool_metadata(self) -> None:
        """Include generated capability descriptions in the request estimate."""
        messages = new_conversation()
        generated_tool = {"function": {"description": "x" * 300}}
        with patch("harness_prototype.agent_loop.MODEL_TOOLS", []):
            without_capabilities = check_context_budget(
                messages, context_length=100_000,
            )
        with patch("harness_prototype.agent_loop.MODEL_TOOLS", [generated_tool]):
            with_capabilities = check_context_budget(
                messages, context_length=100_000,
            )
        self.assertGreater(with_capabilities, without_capabilities)

    def test_repository_research_uses_its_larger_context_profile(self) -> None:
        """Use an adaptive research window without changing ordinary chat capacity."""
        cases = ["Hello there.", "Identify the exact file and function in this repository."]
        observed = []
        for prompt in cases:
            with self.subTest(prompt=prompt):
                client = Mock()
                client.chat.side_effect = RuntimeError("stop after request capture")
                messages = new_conversation() + [{"role": "user", "content": prompt}]
                with patch("harness_prototype.agent_loop.CONTEXT_LENGTH", 4096), patch(
                    "harness_prototype.agent_loop.RESEARCH_CONTEXT_LENGTH",
                    32768,
                ), self.assertRaisesRegex(RuntimeError, "request capture"):
                    run_turn(
                        client,
                        "test",
                        messages,
                        lambda text: None,
                        lambda name: None,
                    )
                observed.append(client.chat.call_args.kwargs["options"]["num_ctx"])
        self.assertEqual(observed[0], 4096)
        self.assertGreaterEqual(observed[1], 4096)
        self.assertLessEqual(observed[1], 32768)
        self.assertEqual(observed[1] % 4096, 0)

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
