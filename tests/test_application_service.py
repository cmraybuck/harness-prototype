"""Focused protocol checks for the localhost application service."""

import http.client
import json
from pathlib import Path
import tempfile
import unittest

from harness_prototype.agent_loop import AgentRuntime
from harness_prototype.application_service import DesktopServiceHost, MAX_REQUEST_BYTES


class ApplicationServiceTests(unittest.TestCase):
    """Cover lifecycle, protocol validation, and ordered tool event streaming."""

    def setUp(self) -> None:
        """Start an isolated loopback service with a simulated runtime."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)

        def simulated_turn(**kwargs: object) -> str:
            """Emit deterministic assistant and tool observations without Ollama."""
            on_text = kwargs["on_text"]
            turn_log = kwargs["turn_log"]
            messages = kwargs["messages"]
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

    def test_health_capabilities_and_loopback_policy(self) -> None:
        """Expose readiness and reject accidental non-loopback listeners."""
        status, health = self.request("GET", "/v1/health")
        self.assertEqual((status, health["status"]), (200, "ready"))
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


if __name__ == "__main__":
    unittest.main()
