"""Focused protocol checks for the localhost application service."""

import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier, Event
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

    def write_skill(self, identifier: str = "fixture.review", **updates: object) -> Path:
        """Install a bounded workspace fixture without changing application skills."""
        directory = self.workspace / ".flux" / "skills" / identifier
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "instructions.md").write_text("Review focused changes.", encoding="utf-8")
        manifest = {"version": 1, "id": identifier, "name": "Fixture review",
                    "description": "Review a fixture.", "instructions": "instructions.md"}
        manifest.update(updates)
        path = directory / "skill.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def restart_host(self) -> None:
        """Restart the isolated backend using the same durable database and factory."""
        factory = self.host.server.registry.runtime_factory
        self.host.stop()
        self.host = DesktopServiceHost(workspace=self.workspace, models=("test-model", "other-model"),
                                       runtime_factory=factory, ollama_status=lambda: {"status": "available"},
                                       state_database_path=self.workspace / "state.sqlite3")
        self.host.start()

    def test_runtime_status_reports_effective_tools_without_model_request(self) -> None:
        """Expose idle context facts and reject busy or missing conversations."""
        _, created = self.request("POST", "/v1/conversations", {})
        identifier = created["conversation_id"]
        runtime = self.host.server.registry.get(identifier)
        before = list(runtime.messages)
        status, result = self.request("GET", f"/v1/conversations/{identifier}/status")
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], runtime.model)
        self.assertEqual(result["message_count"], 0)
        self.assertGreater(result["context_used"], 0)
        self.assertTrue(all(item["description"] for item in result["tools"]))
        self.assertEqual(runtime.messages, before)
        self.assertEqual([item["name"] for item in result["tools"]], ["activate_capabilities"])
        self.assertIn("capabilit", result["tools"][0]["description"].lower())
        health_status, _ = self.request("GET", "/v1/health")
        self.assertEqual(health_status, 200)
        runtime.capability_state.active_groups.add("workspace_inspection")
        status, active = self.request("GET", f"/v1/conversations/{identifier}/status")
        self.assertEqual(status, 200)
        self.assertIn("read_file", [item["name"] for item in active["tools"]])
        self.assertTrue(all(item["description"] for item in active["tools"]))
        self.host.server.registry.begin_turn(identifier, "busy")
        status, _ = self.request("GET", f"/v1/conversations/{identifier}/status")
        self.assertEqual(status, 409)
        self.host.server.registry.abandon_turn(identifier)
        status, _ = self.request("GET", "/v1/conversations/missing/status")
        self.assertEqual(status, 404)

    def test_skill_discovery_defaults_activation_and_restart(self) -> None:
        """Discover metadata, persist only selections, and isolate durable chat activation."""
        path = self.write_skill()
        status, catalog = self.request("GET", "/v1/skills")
        self.assertEqual(status, 200)
        fixture = next(item for item in catalog["skills"] if item["id"] == "fixture.review")
        self.assertTrue(fixture["available"])
        self.assertEqual((fixture["format_version"], fixture["source"]), (1, "workspace"))
        self.assertEqual(Path(fixture["manifest_path"]), path)
        self.assertIn("max_resource_bytes", catalog["limits"])
        self.assertNotIn("Review focused changes.", json.dumps(catalog))
        status, _ = self.request("PATCH", "/v1/skill-preferences",
                                 {"workspace": str(self.workspace), "active_skills": ["fixture.review"]})
        self.assertEqual(status, 200)
        _, first = self.request("POST", "/v1/conversations", {})
        _, second = self.request("POST", "/v1/conversations", {"active_skills": []})
        self.assertEqual(first["active_skills"], ["fixture.review"])
        self.assertEqual(second["active_skills"], [])
        runtime = self.host.server.registry.get(first["conversation_id"])
        self.assertIn("Review focused changes.", runtime.skill_state.context())
        self.assertEqual(first["skill_provenance"][0]["manifest_path"], str(path))
        self.restart_host()
        _, restored = self.request("GET", f"/v1/conversations/{first['conversation_id']}")
        self.assertEqual(restored["active_skills"], ["fixture.review"])
        self.assertEqual(restored["skill_provenance"], first["skill_provenance"])
        self.assertIsNone(restored["skill_error"])
        _, catalog = self.request("GET", "/v1/skills")
        self.assertEqual(catalog["preferred_skills"], ["fixture.review"])
        _, restored_second = self.request("GET", f"/v1/conversations/{second['conversation_id']}")
        self.assertEqual(restored_second["active_skills"], [])

    def test_explicit_skill_reset_validates_confirmation_and_retains_history_and_grants(self) -> None:
        """Validate explicit reset inputs and preserve context on failure."""
        self.write_skill()
        _, created = self.request("POST", "/v1/conversations", {})
        identifier = created["conversation_id"]
        self.request("POST", f"/v1/conversations/{identifier}/turns", {"message": "hello"})
        runtime = self.host.server.registry.get(identifier)
        original = list(runtime.messages)
        original_access = runtime.access
        path = f"/v1/conversations/{identifier}/skills"
        status, error = self.request("PATCH", path, {"active_skills": ["fixture.review"], "confirm_context_reset": "false"})
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(runtime.messages, original)
        status, _ = self.request("PATCH", path, {"active_skills": ["absent"], "confirm_context_reset": True})
        self.assertEqual(status, 409)
        self.assertEqual(runtime.messages, original)
        with patch.object(self.host.server.registry.store, "save_runtime", side_effect=OSError("disk full")):
            status, _ = self.request("PATCH", path, {"active_skills": ["fixture.review"], "confirm_context_reset": True})
        self.assertEqual(status, 503)
        self.assertEqual(runtime.messages, original)
        self.assertEqual(runtime.skill_state.active_ids, ())
        status, changed = self.request("PATCH", path, {"active_skills": ["fixture.review"], "confirm_context_reset": True})
        self.assertEqual((status, changed["active_skills"]), (200, ["fixture.review"]))
        self.assertEqual(len(runtime.messages), 1)
        self.assertIs(runtime.access, original_access)
        _, history = self.request("GET", f"/v1/conversations/{identifier}/history")
        self.assertEqual(history["turns"][0]["user_prompt"], "hello")

    def test_dynamic_skill_activation_and_clear_retain_context_and_capabilities(self) -> None:
        """Match terminal activation without dropping history, capability state, or access."""
        self.write_skill()
        _, created = self.request("POST", "/v1/conversations", {"active_skills": []})
        identifier = created["conversation_id"]
        runtime = self.host.server.registry.get(identifier)
        runtime.capability_state.active_groups.add("workspace_inspection")
        self.request("POST", f"/v1/conversations/{identifier}/turns", {"message": "retained exchange"})
        messages = runtime.messages
        original = list(messages)
        capabilities = runtime.capability_state
        access = runtime.access
        path = f"/v1/conversations/{identifier}/skills"
        status, activated = self.request("PATCH", path,
                                         {"active_skills": ["fixture.review"], "confirm_context_reset": False})
        self.assertEqual((status, activated["active_skills"]), (200, ["fixture.review"]))
        self.assertIs(runtime.messages, messages)
        self.assertEqual(runtime.messages, original)
        self.assertIs(runtime.capability_state, capabilities)
        self.assertIs(runtime.access, access)
        status, _ = self.request("PATCH", path, {"active_skills": ["missing"], "confirm_context_reset": False})
        self.assertEqual(status, 409)
        self.assertEqual(runtime.skill_state.active_ids, ("fixture.review",))
        with patch("harness_prototype.agent_loop.CONTEXT_LENGTH", 1):
            status, error = self.request("PATCH", path, {"active_skills": [], "confirm_context_reset": False})
        self.assertEqual((status, error["error"]["code"]), (409, "skill_context_limit"))
        self.assertEqual(runtime.messages, original)
        self.assertEqual(runtime.skill_state.active_ids, ("fixture.review",))
        status, cleared = self.request("PATCH", path, {"active_skills": [], "confirm_context_reset": False})
        self.assertEqual((status, cleared["active_skills"]), (200, []))
        self.assertEqual(runtime.messages, original)
        self.assertEqual(runtime.capability_state.active_groups, {"workspace_inspection"})

    def test_unavailable_skills_and_context_budget_are_actionable_and_atomic(self) -> None:
        """Expose invalid versions and missing requirements, never partial activation."""
        self.write_skill("fixture.future", version=99)
        self.write_skill("fixture.missing", requires={"capabilities": ["not_configured"], "tools": ["absent_tool"]})
        self.write_skill()
        _, catalog = self.request("GET", "/v1/skills")
        self.assertTrue(any("Unsupported" in item["error"] for item in catalog["diagnostics"]))
        missing = next(item for item in catalog["skills"] if item["id"] == "fixture.missing")
        self.assertFalse(missing["available"])
        self.assertIn("absent_tool", missing["error"])
        _, created = self.request("POST", "/v1/conversations", {"active_skills": ["fixture.review"]})
        path = f"/v1/conversations/{created['conversation_id']}/skills"
        status, _ = self.request("PATCH", path, {"active_skills": ["fixture.review", "fixture.missing"], "confirm_context_reset": True})
        self.assertEqual(status, 409)
        with patch("harness_prototype.agent_loop.CONTEXT_LENGTH", 1):
            status, error = self.request("PATCH", path, {"active_skills": ["fixture.review"], "confirm_context_reset": True})
        self.assertEqual((status, error["error"]["code"]), (409, "skill_context_limit"))
        _, current = self.request("GET", path)
        self.assertEqual(current["active_skills"], ["fixture.review"])
        for invalid in (None, True, "fixture.review", [True], ["fixture.review", "fixture.review"]):
            status, _ = self.request("POST", "/v1/conversations", {"active_skills": invalid})
            self.assertIn(status, (400, 409))

    def test_missing_or_changed_active_source_blocks_turn_after_restart(self) -> None:
        """Retain persisted provenance and require explicit recovery after source changes."""
        path = self.write_skill()
        _, created = self.request("POST", "/v1/conversations", {"active_skills": ["fixture.review"]})
        identifier = created["conversation_id"]
        (path.parent / "instructions.md").write_text("Changed instructions.", encoding="utf-8")
        self.restart_host()
        _, current = self.request("GET", f"/v1/conversations/{identifier}")
        self.assertEqual(current["skill_error"]["status"], "skill_source_changed")
        self.assertEqual(current["skill_provenance"], created["skill_provenance"])
        status, _ = self.request("POST", f"/v1/conversations/{identifier}/turns", {"message": "blocked"})
        self.assertEqual(status, 409)
        status, _ = self.request("PATCH", f"/v1/conversations/{identifier}/skills",
                                 {"active_skills": ["fixture.review"], "confirm_context_reset": True})
        self.assertEqual(status, 200)
        path.unlink()
        self.restart_host()
        _, current = self.request("GET", f"/v1/conversations/{identifier}")
        self.assertEqual(current["active_skills"], ["fixture.review"])
        self.assertEqual(current["skill_error"]["status"], "skill_unavailable")
        status, _ = self.request("PATCH", f"/v1/conversations/{identifier}/skills",
                                 {"active_skills": [], "confirm_context_reset": True})
        self.assertEqual(status, 200)

    def test_skill_selection_cannot_race_an_active_turn_or_grant_authority(self) -> None:
        """Activation leaves policy/access intact and cannot alter a running conversation."""
        self.write_skill(requires={"capabilities": ["command_execution"], "tools": ["run_command"]})
        _, created = self.request("POST", "/v1/conversations", {"active_skills": ["fixture.review"]})
        identifier = created["conversation_id"]
        runtime = self.host.server.registry.get(identifier)
        self.assertNotIn("command_execution", runtime.capability_state.active_groups)
        self.assertFalse(self.host.server.registry.session_tool_grants)
        self.assertFalse(self.host.server.registry.grants.list())
        self.turn_started["waiting"] = Event()
        self.turn_release["waiting"] = Event()
        connection, response, first = self.open_turn_stream(identifier, "waiting")
        self.assertTrue(self.turn_started["waiting"].wait(2))
        status, error = self.request("PATCH", f"/v1/conversations/{identifier}/skills",
                                     {"active_skills": [], "confirm_context_reset": True})
        self.assertEqual((status, error["error"]["code"]), (409, "conversation_busy"))
        self.request("POST", f"/v1/conversations/{identifier}/turns/{first['turn_id']}/cancel", {})
        response.read()
        connection.close()

    def test_concurrent_reads_share_restored_skill_runtime(self) -> None:
        """Hydrate simultaneous history/metadata requests into one conversation state."""
        self.write_skill()
        _, created = self.request("POST", "/v1/conversations", {"active_skills": ["fixture.review"]})
        registry = self.host.server.registry
        identifier = created["conversation_id"]
        registry._runtimes.clear()
        original_factory = registry.runtime_factory
        ready = Barrier(2)

        def factory(model: str, workspace: Path) -> AgentRuntime:
            """Force both readers to finish construction before either retains a runtime."""
            runtime = original_factory(model, workspace)
            ready.wait(timeout=5)
            return runtime

        with patch.object(registry, "runtime_factory", factory):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(registry.get, identifier) for _ in range(2)]
                first, second = [future.result(timeout=5) for future in futures]
        self.assertIs(first, second)
        self.assertEqual(first.skill_state.active_ids, ("fixture.review",))

    def test_existing_skill_view_rediscovers_installed_sources_without_changing_context(self) -> None:
        """Find newly installed skills while retaining the loaded conversation snapshot."""
        _, created = self.request("POST", "/v1/conversations", {})
        identifier = created["conversation_id"]
        runtime = self.host.server.registry.get(identifier)
        previous_messages = list(runtime.messages)
        self.write_skill()
        _, catalog = self.request("GET", f"/v1/conversations/{identifier}/skills")
        self.assertTrue(any(item["id"] == "fixture.review" for item in catalog["skills"]))
        self.assertEqual(catalog["active_skills"], [])
        self.assertEqual(runtime.messages, previous_messages)
        self.assertNotIn("fixture.review", runtime.skill_catalog.skills)

    def test_session_state_search_preferences_and_active_guards(self) -> None:
        """Persist scoped session state and reject active lifecycle changes and invalid preferences."""
        _, first = self.request("POST", "/v1/conversations", {})
        owner = first["conversation_id"]
        path = f"/v1/conversations/{owner}"
        for field, value in (("draft", "Unsubmitted text"), ("pinned", True), ("archived", True)):
            status, metadata = self.request("PATCH", path, {field: value})
            self.assertEqual(status, 200)
            self.assertEqual(metadata[field], value)
        status, _ = self.request("POST", path + "/turns", {"message": "Cannot send archived"})
        self.assertEqual(status, 409)
        self.request("PATCH", path, {"archived": False})
        registry = self.host.server.registry
        registry.begin_turn(owner, "reserved")
        for method, body in (("PATCH", {"archived": True}), ("DELETE", None)):
            status, error = self.request(method, path, body)
            self.assertEqual((status, error["error"]["code"]), (409, "conversation_busy"))
        self.assertEqual(self.request("PATCH", path, {"draft": "Typing during work"})[0], 200)
        registry.finish_turn(owner)
        self.request("POST", path + "/turns", {"message": "Session search prompt"})
        self.request("PATCH", path, {"archived": True})
        status, results = self.request("GET", "/v1/conversations?archived=false&search=search+prompt")
        self.assertEqual(status, 200)
        self.assertEqual(results["conversations"][0]["conversation_id"], owner)
        self.assertTrue(results["conversations"][0]["archived"])
        self.assertEqual(self.request("GET", "/v1/conversations?archived=false")[1]["conversations"], [])
        for body in ({"pinned": 1}, {"archived": "true"}, {"draft": "x" * 16001}):
            self.assertEqual(self.request("PATCH", path, body)[0], 400)
        preferences = {"text_size": "large", "zoom_percent": 125, "reduced_motion": True, "sidebar_collapsed": True, "sidebar_width": 290,
                       "window": {"width": 1000, "height": 700, "x": -800, "y": 20, "maximized": False}}
        self.assertEqual(self.request("PATCH", "/v1/presentation", preferences), (200, preferences))
        for invalid in ({"text_size": []}, {"zoom_percent": True}, {"zoom_percent": 125.0}, {"zoom_percent": 101},
                        {"sidebar_width": True}, {"reduced_motion": 1}, {"unknown": True}):
            self.assertEqual(self.request("PATCH", "/v1/presentation", invalid)[0], 400)
        self.restart_host()
        self.assertEqual(self.request("GET", "/v1/presentation"), (200, preferences))
        self.assertTrue(self.request("GET", path)[1]["archived"])

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

    def test_token_stream_batches_durable_writes_and_preserves_order(self) -> None:
        """Avoid a durable database transaction per token without losing streamed text."""
        handler, runtime = MagicMock(), MagicMock()
        runtime.conversation_id = "one"
        stream = TurnEventStream(handler, runtime, "turn")
        with patch("harness_prototype.application_service.monotonic", return_value=1.0):
            for _ in range(1000):
                stream.emit("assistant_text", {"text": "x"})
            stream.emit("completion", {"outcome": "success"})
        events = [call.args[2]["public_event"] for call in
                  handler.server.registry.store.save_turn_event.call_args_list]
        self.assertLessEqual(len(events), 5)
        self.assertEqual("".join(event.get("text", "") for event in events), "x" * 1000)
        self.assertEqual(events[-1]["type"], "completion")
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))

    def test_text_batch_flushes_on_time_size_and_disconnect(self) -> None:
        """Bound batches and persist pending text before terminal disconnect recovery."""
        handler, runtime = MagicMock(), MagicMock()
        runtime.conversation_id = "one"
        stream = TurnEventStream(handler, runtime, "turn")
        with patch("harness_prototype.application_service.monotonic", return_value=1.0):
            stream.emit("assistant_text", {"text": "first"})
            self.assertEqual(stream.sequence, 1)
            stream.emit("assistant_text", {"text": "pending"})
            self.assertEqual(stream.sequence, 1)
        with patch("harness_prototype.application_service.monotonic", return_value=1.2):
            stream.emit("assistant_text", {"text": " timed"})
            self.assertEqual(stream.sequence, 2)
            stream.emit("assistant_text", {"text": "x" * 4096})
            self.assertEqual(stream.sequence, 3)
            stream.emit("assistant_text", {"text": "tail"})
            handler.wfile.write.side_effect = BrokenPipeError
            stream.emit("completion", {"outcome": "cancelled"})
        events = [call.args[2]["public_event"] for call in
                  handler.server.registry.store.save_turn_event.call_args_list]
        self.assertEqual(events[1]["text"], "pending timed")
        self.assertEqual(events[-2]["text"], "tail")
        self.assertEqual(events[-1]["type"], "completion")
        runtime.interrupt.assert_called_once_with()

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
        self.write_skill(requires={"capabilities": ["documentation"], "tools": ["fetch_documentation"]})
        _, created = self.request("POST", "/v1/conversations", {"active_skills": ["fixture.review"]})
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

    def test_expired_request_cannot_be_approved_before_waiter_cleanup(self) -> None:
        """Reject late decisions even before the permission waiter removes its registry entry."""
        broker = PermissionBroker()
        with patch("harness_prototype.application_service.monotonic", return_value=0) as clock:
            def emit(kind: str, fields: dict) -> None:
                """Advance beyond the original deadline during deterministic publication."""
                if kind == "permission_request":
                    clock.return_value = 301
                    self.assertFalse(broker.resolve(fields["permission_request_id"], "allow_once"))
                else:
                    self.assertEqual(fields["decision"], "deny")
            self.assertIsNone(broker.request({"choices": ["allow_once", "deny"]}, emit, lambda: True))

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
