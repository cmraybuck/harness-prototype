"""Focused protocol checks for the localhost application service."""

import http.client
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from harness_prototype.agent_loop import AgentRuntime, RuntimeInterrupted
from harness_prototype.application_service import (
    DesktopServiceHost, MAX_REQUEST_BYTES, PermissionBroker, TurnEventStream, _permission_action_summary,
)
from harness_prototype.approval import ApprovalRequest, ToolCategory
from harness_prototype.locations import get_location


class ApplicationServiceTests(unittest.TestCase):
    """Cover lifecycle, protocol validation, and ordered tool event streaming."""

    def setUp(self) -> None:
        """Start an isolated loopback service with a simulated runtime."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.turn_started: dict[str, Event] = {}
        self.turn_release: dict[str, Event] = {}
        self.observed_workspaces: dict[str, str] = {}

        def simulated_turn(**kwargs: object) -> str:
            """Emit deterministic assistant and tool observations without Ollama."""
            on_text = kwargs["on_text"]
            turn_log = kwargs["turn_log"]
            messages = kwargs["messages"]
            prompt = messages[-1]["content"]
            self.observed_workspaces[prompt] = get_location("working_directory")["path"]
            if prompt in self.turn_started:
                self.turn_started[prompt].set()
                while not self.turn_release[prompt].wait(0.01):
                    if kwargs["is_interrupted"]():
                        raise RuntimeInterrupted
            on_text("Checking.")
            turn_log.add_event("tool_call", name="list_files", arguments={"path": "."})
            result = {"ok": True, "complete": True, "entries": []}
            turn_log.add_event("tool_result", name="list_files", duration_ms=1, result=result)
            on_text(" Done.")
            messages.append({"role": "assistant", "content": "Checking. Done."})
            return "success"

        def runtime_factory(model: str, workspace: Path) -> AgentRuntime:
            """Build one runtime around the deterministic turn runner."""
            return AgentRuntime(object(), model, workspace, turn_runner=simulated_turn)

        self.host = DesktopServiceHost(
            workspace=self.workspace,
            models=("test-model", "other-model"),
            runtime_factory=runtime_factory,
            ollama_status=lambda: {"status": "available"},
            state_database_path=self.workspace / "state.sqlite3",
        )
        self.host.start()

    def tearDown(self) -> None:
        """Stop the listener and remove its temporary workspace."""
        self.host.stop()
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> tuple[int, dict | list[dict]]:
        """Send one JSON request and decode JSON or NDJSON output."""
        connection = http.client.HTTPConnection(*self.host.address, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        connection.close()
        decoded = (
            [json.loads(line) for line in raw.splitlines()]
            if content_type.startswith("application/x-ndjson")
            else json.loads(raw)
        )
        return response.status, decoded

    def open_turn_stream(
        self, conversation_id: str, message: str,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, dict]:
        """Open a turn stream and return its first identified event."""
        connection = http.client.HTTPConnection(*self.host.address, timeout=5)
        payload = json.dumps({"message": message}).encode("utf-8")
        connection.request(
            "POST", f"/v1/conversations/{conversation_id}/turns", body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        first_event = json.loads(response.fp.readline().decode("utf-8"))
        return connection, response, first_event

    def test_health_capabilities_and_loopback_policy(self) -> None:
        """Expose readiness and reject accidental non-loopback listeners."""
        status, health = self.request("GET", "/v1/health")
        self.assertEqual((status, health["status"]), (200, "ready"))
        self.assertEqual(health["ollama"], {"status": "available"})
        status, capabilities = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertIn("tool_result", capabilities["event_types"])
        with self.assertRaisesRegex(ValueError, "allow_remote"):
            DesktopServiceHost("0.0.0.0", workspace=self.workspace)

    def test_simulated_tool_turn_streams_ordered_stable_events(self) -> None:
        """Complete a tool-using turn with portable IDs and ordered semantics."""
        status, conversation = self.request(
            "POST", "/v1/conversations", {"model": "test-model", "workspace": str(self.workspace)}
        )
        self.assertEqual(status, 201)
        conversation_id = conversation["conversation_id"]

        status, events = self.request(
            "POST", f"/v1/conversations/{conversation_id}/turns", {"message": "Inspect files"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [event["type"] for event in events],
            ["turn_started", "assistant_text", "tool_call", "tool_result", "assistant_text", "completion"],
        )
        self.assertEqual([event["sequence"] for event in events], list(range(1, 7)))
        self.assertEqual(events[2]["tool_call_id"], events[3]["tool_call_id"])
        self.assertTrue(all(event["conversation_id"] == conversation_id for event in events))
        self.assertEqual(len({event["turn_id"] for event in events}), 1)

    def test_history_recovers_exact_public_stream_without_raw_arguments(self) -> None:
        """Persist the complete display sequence for reconnecting GUI clients."""
        _, conversation = self.request(
            "POST", "/v1/conversations", {"model": "test-model", "workspace": str(self.workspace)}
        )
        conversation_id = conversation["conversation_id"]
        _, events = self.request(
            "POST", f"/v1/conversations/{conversation_id}/turns", {"message": "Inspect files"}
        )
        _, history = self.request("GET", f"/v1/conversations/{conversation_id}/history")
        recovered = history["turns"][0]["public_events"]
        self.assertEqual(recovered, events)
        self.assertEqual(history["turns"][0]["user_prompt"], "Inspect files")
        self.assertNotIn("arguments", recovered[2])

    def test_disconnected_stream_retains_terminal_events_for_recovery(self) -> None:
        """Persist interruption and completion even after a failed socket write."""
        handler, runtime = MagicMock(), MagicMock()
        runtime.conversation_id = "one"
        handler.wfile.write.side_effect = BrokenPipeError
        stream = TurnEventStream(handler, runtime, "turn")
        stream.emit("turn_started")
        stream.emit("interruption", {"message": "Cancelled"})
        stream.emit("completion", {"outcome": "cancelled"})
        runtime.interrupt.assert_called_once_with()
        events = [call.args[2]["public_event"] for call in
                  handler.server.registry.store.save_turn_event.call_args_list]
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        self.assertEqual(events[-1]["type"], "completion")

    def test_invalid_requests_are_structured_and_recoverable(self) -> None:
        """Return bounded protocol errors without damaging later requests."""
        status, error = self.request("POST", "/v1/conversations", {"model": "missing"})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_model")
        self.assertTrue(error["error"]["recoverable"])

        connection = http.client.HTTPConnection(*self.host.address, timeout=5)
        connection.request(
            "POST",
            "/v1/conversations",
            body=b"{}",
            headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)},
        )
        response = connection.getresponse()
        oversized = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 413)
        self.assertEqual(oversized["error"]["code"], "request_too_large")

    def test_durable_grants_can_be_listed_and_revoked_without_payloads(self) -> None:
        """Expose grant administration while keeping lifecycle audit rows redacted."""
        grant = self.host.server.registry.grants.create(
            "grant-1", "directory", str(self.workspace.resolve()), str(self.workspace.resolve())
        )
        status, listed = self.request("GET", "/v1/grants")
        self.assertEqual(status, 200)
        self.assertEqual(listed["grants"], [grant])

        status, revoked = self.request("DELETE", "/v1/grants/grant-1")
        self.assertEqual((status, revoked["status"]), (200, "revoked"))
        status, listed = self.request("GET", "/v1/grants")
        self.assertEqual((status, listed["grants"]), (200, []))
        status, missing = self.request("DELETE", "/v1/grants/grant-1")
        self.assertEqual((status, missing["error"]["code"]), (404, "grant_not_found"))

    def test_settings_validate_persist_and_select_default_model(self) -> None:
        """Save safe preferences without changing existing conversation selections."""
        preferences = {"default_model": "other-model", "backend_host": "http://localhost:11435"}
        status, saved = self.request("PATCH", "/v1/settings", preferences)
        self.assertEqual(status, 200)
        self.assertEqual(saved["default_model"], "other-model")
        status, created = self.request("POST", "/v1/conversations", {})
        self.assertEqual(created["model"], "other-model")
        for host in ("http://user:secret@localhost", "http://localhost?token=secret", "file:///private", "http://localhost:bad"):
            status, rejected = self.request("PATCH", "/v1/settings", {**preferences, "backend_host": host})
            self.assertEqual(status, 400)
            self.assertNotIn("secret", json.dumps(rejected))
        self.assertEqual(self.host.server.registry.grants.state.get("desktop", "preferences"), preferences)
        with patch("harness_prototype.application_service.Client") as client:
            restarted = DesktopServiceHost(workspace=self.workspace, models=("test-model", "other-model"),
                                           state_database_path=self.workspace / "state.sqlite3")
            self.addCleanup(restarted.stop)
            restarted.server.registry.runtime_factory("other-model", self.workspace)
            client.assert_called_once_with(host="http://localhost:11435")
        self.request("PATCH", "/v1/settings", {**preferences, "default_model": "test-model"})
        status, current = self.request("GET", f"/v1/conversations/{created['conversation_id']}")
        self.assertEqual(current["model"], "other-model")

    def test_gui_permission_scopes_reuse_and_revocation(self) -> None:
        """Complete allowed and denied tool turns with exact scope reuse and revocation."""
        outcomes = []

        def permission_turn(**kwargs: object) -> str:
            """Simulate a sensitive request while exposing only its approval result."""
            decision = kwargs["on_approval"](ApprovalRequest(
                "fetch_documentation", {"url": "https://example.com/?credential=private-secret"}, ToolCategory.NETWORK,
            ))
            outcomes.append(bool(decision))
            return "success"

        def factory(model: str, workspace: Path) -> AgentRuntime:
            """Build a deterministic runtime for GUI approval integration."""
            return AgentRuntime(object(), model, workspace, turn_runner=permission_turn)

        self.host.server.registry.runtime_factory = factory
        _, created = self.request("POST", "/v1/conversations", {})
        owner = created["conversation_id"]
        for choice in ("allow_once", "deny", "allow_durable"):
            connection, response, _ = self.open_turn_stream(owner, choice)
            self.addCleanup(connection.close)
            request = json.loads(response.fp.readline())
            self.assertEqual(request["type"], "permission_request")
            self.assertNotIn("private-secret", json.dumps(request))
            self.assertEqual(request["scope"], "All operations in risk category: network")
            status, _ = self.request("POST", f"/v1/permissions/{request['permission_request_id']}", {"decision": choice})
            self.assertEqual(status, 200)
            events = [json.loads(line) for line in response.read().splitlines()]
            connection.close()
            self.assertEqual(next(event for event in events if event["type"] == "permission_receipt")["decision"], choice)
        self.assertEqual(outcomes, [True, False, True])
        _, reused = self.request("POST", f"/v1/conversations/{owner}/turns", {"message": "reuse"})
        self.assertNotIn("permission_request", [event["type"] for event in reused])
        _, grants = self.request("GET", "/v1/grants")
        self.request("DELETE", f"/v1/grants/{grants['grants'][0]['grant_id']}")
        connection, response, _ = self.open_turn_stream(owner, "after revoke")
        self.addCleanup(connection.close)
        requested = json.loads(response.fp.readline())
        self.assertEqual(requested["type"], "permission_request")
        self.request("POST", f"/v1/permissions/{requested['permission_request_id']}", {"decision": "allow_session"})
        response.read()
        connection.close()
        _, reused = self.request("POST", f"/v1/conversations/{owner}/turns", {"message": "session reuse"})
        self.assertTrue(any(event["type"] == "permission_grant" and event["lifetime"] == "session" for event in reused))

    def test_cancellation_denies_pending_permission_promptly(self) -> None:
        """Interrupt a waiting permission without allowing its tool or waiting for timeout."""
        outcomes = []

        def permission_turn(**kwargs: object) -> str:
            """Wait on the backend approval callback and record whether it allowed."""
            decision = kwargs["on_approval"](ApprovalRequest("fetch_documentation", {}, ToolCategory.NETWORK))
            outcomes.append(bool(decision))
            if kwargs["is_interrupted"]():
                raise RuntimeInterrupted
            return "success"

        def factory(model: str, workspace: Path) -> AgentRuntime:
            """Build a cancellation-aware simulated approval workflow."""
            return AgentRuntime(object(), model, workspace, turn_runner=permission_turn)

        self.host.server.registry.runtime_factory = factory
        _, created = self.request("POST", "/v1/conversations", {})
        owner = created["conversation_id"]
        connection, response, started = self.open_turn_stream(owner, "cancel approval")
        self.addCleanup(connection.close)
        requested = json.loads(response.fp.readline())
        self.request("POST", f"/v1/conversations/{owner}/turns/{started['turn_id']}/cancel", {})
        events = [json.loads(line) for line in response.read().splitlines()]
        self.assertEqual(outcomes, [False])
        self.assertEqual(events[-1]["outcome"], "cancelled")
        status, _ = self.request("POST", f"/v1/permissions/{requested['permission_request_id']}", {"decision": "allow_once"})
        self.assertEqual(status, 404)

    def test_durable_tool_grant_suppresses_matching_prompt_after_restart(self) -> None:
        """Reuse only a matching durable tool scope after reopening application state."""
        database = self.workspace / "durable-state.sqlite3"

        def permission_turn(**kwargs: object) -> str:
            """Request one deterministic network-category approval."""
            decision = kwargs["on_approval"](ApprovalRequest(
                "fetch_documentation", {"url": "https://example.com/private?token=secret"},
                ToolCategory.NETWORK,
            ))
            kwargs["messages"].append({"role": "assistant", "content": str(bool(decision))})
            return "success"

        def runtime_factory(model: str, workspace: Path) -> AgentRuntime:
            """Build a runtime whose turn exercises the approval callback."""
            return AgentRuntime(object(), model, workspace, turn_runner=permission_turn)

        first_host = DesktopServiceHost(
            workspace=self.workspace, models=("test-model",),
            runtime_factory=runtime_factory, state_database_path=database,
        )
        first_host.start()
        try:
            first_connection = http.client.HTTPConnection(*first_host.address, timeout=5)
            first_connection.request(
                "POST", "/v1/conversations", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            created = json.loads(first_connection.getresponse().read())
            first_connection.close()
            connection = http.client.HTTPConnection(*first_host.address, timeout=5)
            connection.request(
                "POST", f"/v1/conversations/{created['conversation_id']}/turns",
                body=b'{"message":"fetch"}', headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            json.loads(response.fp.readline())
            requested = json.loads(response.fp.readline())
            self.assertEqual(requested["type"], "permission_request")
            resolver = http.client.HTTPConnection(*first_host.address, timeout=5)
            resolver.request(
                "POST", f"/v1/permissions/{requested['permission_request_id']}",
                body=b'{"decision":"allow_durable"}',
                headers={"Content-Type": "application/json"},
            )
            resolved_response = resolver.getresponse()
            self.assertEqual(resolved_response.status, 200)
            resolved_response.read()
            resolver.close()
            response.read()
            connection.close()
        finally:
            first_host.stop()

        second_host = DesktopServiceHost(
            workspace=self.workspace, models=("test-model",),
            runtime_factory=runtime_factory, state_database_path=database,
        )
        second_host.start()
        try:
            connection = http.client.HTTPConnection(*second_host.address, timeout=5)
            connection.request(
                "POST", "/v1/conversations", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            created = json.loads(connection.getresponse().read())
            connection.close()
            connection = http.client.HTTPConnection(*second_host.address, timeout=5)
            connection.request(
                "POST", f"/v1/conversations/{created['conversation_id']}/turns",
                body=b'{"message":"fetch again"}',
                headers={"Content-Type": "application/json"},
            )
            events = [
                json.loads(line) for line in connection.getresponse().read().splitlines()
            ]
            connection.close()
        finally:
            second_host.stop()
        self.assertNotIn("permission_request", [event["type"] for event in events])
        self.assertIn("permission_grant", [event["type"] for event in events])

    def test_conversation_creation_requires_a_configured_model(self) -> None:
        """Return actionable recovery guidance when model selection is unconfigured."""
        empty_host = DesktopServiceHost(
            workspace=self.workspace,
            models=(),
            state_database_path=self.workspace / "empty-state.sqlite3",
        )
        empty_host.start()
        try:
            connection = http.client.HTTPConnection(*empty_host.address, timeout=5)
            connection.request(
                "POST",
                "/v1/conversations",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            error = json.loads(response.read())
            connection.close()
        finally:
            empty_host.stop()
        self.assertEqual(response.status, 409)
        self.assertEqual(error["error"]["code"], "model_configuration_required")
        self.assertTrue(error["error"]["recoverable"])
        self.assertIn("HARNESS_MODELS", error["error"]["message"])

        status, health = self.request("GET", "/v1/health")
        self.assertEqual((status, health["status"]), (200, "ready"))

    def test_model_and_workspace_selection_reset_the_conversation(self) -> None:
        """Drive both selection operations through the versioned API."""
        _, conversation = self.request("POST", "/v1/conversations", {})
        conversation_id = conversation["conversation_id"]
        status, changed = self.request(
            "PATCH", f"/v1/conversations/{conversation_id}", {"model": "other-model"}
        )
        self.assertEqual((status, changed["model"]), (200, "other-model"))
        status, changed = self.request(
            "PATCH", f"/v1/conversations/{conversation_id}", {"workspace": str(self.workspace)}
        )
        self.assertEqual((status, Path(changed["workspace"])), (200, self.workspace.resolve()))

    def test_conversations_persist_list_rename_resume_and_delete(self) -> None:
        """Manage durable chats and restore exact retained history after restart."""
        _, first = self.request("POST", "/v1/conversations", {})
        _, second = self.request("POST", "/v1/conversations", {"model": "other-model"})
        status, renamed = self.request(
            "PATCH", f"/v1/conversations/{first['conversation_id']}", {"title": "Repository review"}
        )
        self.assertEqual((status, renamed["title"]), (200, "Repository review"))
        self.request(
            "POST", f"/v1/conversations/{first['conversation_id']}/turns", {"message": "persist me"}
        )

        status, page = self.request("GET", "/v1/conversations?limit=1")
        self.assertEqual(status, 200)
        self.assertIsNotNone(page["next_cursor"])
        status, following = self.request(
            "GET", f"/v1/conversations?limit=1&after={page['next_cursor']}"
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(
            page["conversations"][0]["conversation_id"],
            following["conversations"][0]["conversation_id"],
        )
        status, history = self.request(
            "GET", f"/v1/conversations/{first['conversation_id']}/history?limit=1"
        )
        self.assertEqual((status, history["turns"][0]["status"]), (200, "success"))
        self.assertEqual(
            [event["type"] for event in history["turns"][0]["events"]],
            ["tool_call", "tool_result"],
        )

        runtime_factory = self.host.server.registry.runtime_factory
        self.host.stop()
        self.host = DesktopServiceHost(
            workspace=self.workspace,
            models=("test-model", "other-model"),
            runtime_factory=runtime_factory,
            state_database_path=self.workspace / "state.sqlite3",
        )
        self.host.start()
        status, restored = self.request(
            "GET", f"/v1/conversations/{first['conversation_id']}"
        )
        self.assertEqual((status, restored["title"]), (200, "Repository review"))
        runtime = self.host.server.registry.get(first["conversation_id"])
        self.assertEqual(runtime.messages[-2]["content"], "persist me")
        self.assertEqual(runtime.messages[-1]["content"], "Checking. Done.")

        status, deleted = self.request(
            "DELETE", f"/v1/conversations/{second['conversation_id']}"
        )
        self.assertEqual((status, deleted["status"]), (200, "deleted"))
        status, missing = self.request("GET", f"/v1/conversations/{second['conversation_id']}")
        self.assertEqual((status, missing["error"]["code"]), (404, "conversation_not_found"))

    def test_conversations_run_concurrently_without_mixing_history(self) -> None:
        """Allow independent runtimes to overlap while retaining separate prompts."""
        first_workspace = self.workspace / "first"
        second_workspace = self.workspace / "second"
        first_workspace.mkdir()
        second_workspace.mkdir()
        _, first = self.request(
            "POST", "/v1/conversations", {"workspace": str(first_workspace)}
        )
        _, second = self.request(
            "POST", "/v1/conversations", {"workspace": str(second_workspace)}
        )
        self.turn_started = {"first": Event(), "second": Event()}
        self.turn_release = {"first": Event(), "second": Event()}

        first_connection, first_response, _ = self.open_turn_stream(first["conversation_id"], "first")
        second_connection, second_response, _ = self.open_turn_stream(second["conversation_id"], "second")
        self.assertTrue(self.turn_started["first"].wait(1))
        self.assertTrue(self.turn_started["second"].wait(1))
        self.turn_release["first"].set()
        self.turn_release["second"].set()
        first_response.read()
        second_response.read()
        first_connection.close()
        second_connection.close()

        first_runtime = self.host.server.registry.get(first["conversation_id"])
        second_runtime = self.host.server.registry.get(second["conversation_id"])
        self.assertEqual(first_runtime.messages[1]["content"], "first")
        self.assertEqual(second_runtime.messages[1]["content"], "second")
        self.assertEqual(Path(self.observed_workspaces["first"]), first_workspace.resolve())
        self.assertEqual(Path(self.observed_workspaces["second"]), second_workspace.resolve())

    def test_turn_conflicts_and_cancellation_use_stable_turn_identity(self) -> None:
        """Reject overlap and prevent stale cancellation from reaching another turn."""
        _, conversation = self.request("POST", "/v1/conversations", {})
        conversation_id = conversation["conversation_id"]
        self.turn_started = {"blocked": Event()}
        self.turn_release = {"blocked": Event()}
        connection, response, started = self.open_turn_stream(conversation_id, "blocked")
        self.assertTrue(self.turn_started["blocked"].wait(1))

        status, busy = self.request(
            "POST", f"/v1/conversations/{conversation_id}/turns", {"message": "overlap"}
        )
        self.assertEqual((status, busy["error"]["code"]), (409, "conversation_busy"))
        status, stale = self.request(
            "POST", f"/v1/conversations/{conversation_id}/turns/stale/cancel", {}
        )
        self.assertEqual((status, stale["error"]["code"]), (404, "turn_not_active"))
        status, cancelled = self.request(
            "POST",
            f"/v1/conversations/{conversation_id}/turns/{started['turn_id']}/cancel",
            {},
        )
        self.assertEqual((status, cancelled["turn_id"]), (202, started["turn_id"]))

        remaining = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
        connection.close()
        self.assertEqual([event["type"] for event in remaining[-2:]], ["interruption", "completion"])
        runtime = self.host.server.registry.get(conversation_id)
        self.assertNotIn("blocked", [message.get("content") for message in runtime.messages])

    def test_cancelling_one_conversation_does_not_interrupt_another(self) -> None:
        """Route cancellation to one identified turn while another completes."""
        _, first = self.request("POST", "/v1/conversations", {})
        _, second = self.request("POST", "/v1/conversations", {})
        self.turn_started = {"cancel me": Event(), "finish me": Event()}
        self.turn_release = {"cancel me": Event(), "finish me": Event()}
        first_connection, first_response, first_started = self.open_turn_stream(
            first["conversation_id"], "cancel me"
        )
        second_connection, second_response, _ = self.open_turn_stream(
            second["conversation_id"], "finish me"
        )
        self.assertTrue(self.turn_started["cancel me"].wait(1))
        self.assertTrue(self.turn_started["finish me"].wait(1))

        status, _ = self.request(
            "POST",
            f"/v1/conversations/{first['conversation_id']}/turns/"
            f"{first_started['turn_id']}/cancel",
            {},
        )
        self.assertEqual(status, 202)
        self.turn_release["finish me"].set()
        first_events = [
            json.loads(line) for line in first_response.read().decode("utf-8").splitlines()
        ]
        second_events = [
            json.loads(line) for line in second_response.read().decode("utf-8").splitlines()
        ]
        first_connection.close()
        second_connection.close()
        self.assertEqual(first_events[-1]["outcome"], "cancelled")
        self.assertEqual(second_events[-1]["outcome"], "success")

    def test_shutdown_interrupts_and_releases_an_abandoned_turn(self) -> None:
        """Recover active history and release its reservation during backend shutdown."""
        _, conversation = self.request("POST", "/v1/conversations", {})
        conversation_id = conversation["conversation_id"]
        self.turn_started = {"abandoned": Event()}
        self.turn_release = {"abandoned": Event()}
        connection, response, _ = self.open_turn_stream(conversation_id, "abandoned")
        self.assertTrue(self.turn_started["abandoned"].wait(1))

        self.host.stop()

        remaining = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
        connection.close()
        self.assertEqual([event["type"] for event in remaining[-2:]], ["interruption", "completion"])
        runtime = self.host.server.registry.get(conversation_id)
        self.assertNotIn("abandoned", [message.get("content") for message in runtime.messages])
        self.assertEqual(self.host.server.registry._active_turns, {})


class PermissionBrokerTests(unittest.TestCase):
    """Exercise decision validation and cancellation without network timing races."""

    def test_file_action_summary_omits_contents_and_revisions(self) -> None:
        """Show counts and actions while keeping write payloads private."""
        summary = _permission_action_summary(ApprovalRequest("apply_file_changes", {"changes": [
            {"action": "overwrite", "content": "private-secret", "expected_revision": "private-secret"},
            {"action": "append", "content": "private-secret"},
        ]}, ToolCategory.WRITE))
        self.assertIn("2 file changes", summary)
        self.assertIn("overwrite: 1", summary)
        self.assertIn("append: 1", summary)
        self.assertNotIn("private-secret", summary)

    def test_only_advertised_first_decision_can_resolve(self) -> None:
        """Reject broader choices and repeat resolutions for the same request."""
        broker = PermissionBroker()
        events = []

        def emit(kind: str, fields: dict) -> None:
            """Resolve during publication so the outcome is deterministic."""
            events.append((kind, fields))
            if kind == "permission_request":
                request_id = fields["permission_request_id"]
                self.assertFalse(broker.resolve(request_id, "allow_durable"))
                self.assertTrue(broker.resolve(request_id, "deny"))
                self.assertFalse(broker.resolve(request_id, "allow_once"))

        self.assertEqual(broker.request({"choices": ["allow_once", "deny"]}, emit, lambda: False), "deny")
        self.assertEqual(events[-1][0], "permission_receipt")

    def test_disconnect_wins_over_concurrent_allow(self) -> None:
        """Never return an allow when the owning stream has been disconnected."""
        broker = PermissionBroker()
        cancelled = Event()

        def emit(kind: str, fields: dict) -> None:
            """Simulate disconnection just after a racing approval submission."""
            if kind == "permission_request":
                broker.resolve(fields["permission_request_id"], "allow_once")
                cancelled.set()
            else:
                self.assertEqual(fields["decision"], "deny")

        self.assertIsNone(broker.request({"choices": ["allow_once", "deny"]}, emit, cancelled.is_set))


if __name__ == "__main__":
    unittest.main()
