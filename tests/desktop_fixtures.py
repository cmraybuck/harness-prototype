"""Deterministic FLUX visual fixtures; no service, model, or personal state is accessed."""

import argparse
from collections.abc import Callable
from pathlib import Path
import tkinter as tk
from unittest.mock import patch

from harness_prototype.desktop import DesktopApiError, FluxDesktop
from harness_prototype.desktop_platform import display_bounds


STATES = ("empty", "active", "working", "permission", "settings", "skills", "error", "long", "sessions")


class FixtureClient:
    """Supply isolated display metadata without starting the backend or touching SQLite."""

    def __init__(self) -> None:
        """Create a small realistic catalog with pinned, recent, and archived sessions."""
        self.items = [{"conversation_id": "session", "title": "Review the local agent", "model": "fixture-model",
                       "workspace": "C:/Projects/sample", "active_skills": [], "draft": "", "pinned": True, "archived": False},
                      {"conversation_id": "recent", "title": "Investigate the connection error", "pinned": False, "archived": False},
                      {"conversation_id": "archive", "title": "Last week's implementation", "pinned": False, "archived": True}]
        for item in self.items:
            item.setdefault("model", "fixture-model")
            item.setdefault("workspace", "C:/Projects/sample")
            item.setdefault("active_skills", [])
            item.setdefault("draft", "")
        self.grant_active = True
        self.preferences = {"default_model": "fixture-model", "backend_host": "http://localhost:11434"}

    def health(self) -> dict:
        """Return readiness without contacting Ollama."""
        return {"ollama": {"status": "available"}}

    def capabilities(self) -> dict:
        """Return a single fake configured model."""
        return {"models": ["fixture-model"]}

    def presentation(self) -> dict:
        """Use the standard appearance without reading local settings."""
        return {}

    def list_conversations(self, *, archived: bool = False, search: str = "") -> list[dict]:
        """Return fixture sessions with illustrative cross-archive snippets."""
        if search:
            return [{**item, "snippet": "A matching message about the local implementation…"} for item in self.items]
        return [item for item in self.items if item["archived"] == archived]

    def get_conversation(self, owner: str) -> dict:
        """Return fixture metadata for a stable session identity."""
        return next(item for item in self.items if item["conversation_id"] == owner)

    def history(self, owner: str) -> list:
        """Return empty fixture history; the builder injects ordered display events."""
        return []

    def settings(self) -> dict:
        """Return illustrative credential-free backend preferences."""
        return dict(self.preferences)

    def list_grants(self) -> list[dict]:
        """Return one scoped demonstration grant without accessing real permissions."""
        if not self.grant_active:
            return []
        return [{"grant_id": "fixture-grant", "kind": "directory", "target": "C:/Projects/sample", "workspace": "C:/Projects/sample"}]

    def skills(self, workspace: Path, owner: str | None = None) -> dict:
        """Return illustrative installed-skill metadata and loading limits."""
        return {"active_skills": [], "skills": [{"id": "review", "name": "Code review", "available": True,
                 "description": "Inspect changes and explain concrete correctness findings.", "source": "Local", "validation_state": "valid"}],
                 "roots": ["C:/Projects/sample/skills"], "limits": {"max_active": 4}}

    def save_presentation(self, values: dict) -> dict:
        """Leave fixture settings in memory rather than writing personal state."""
        return values

    def session_state(self, owner: str, field: str, value: object) -> dict:
        """Update only the ephemeral fixture metadata."""
        metadata = self.get_conversation(owner)
        metadata[field] = value
        return metadata

    def save_settings(self, model: str, host: str) -> dict:
        """Acknowledge fixture preferences without touching the configured Ollama host."""
        self.preferences = {"default_model": model, "backend_host": host}
        return dict(self.preferences)

    def revoke_grant(self, grant_id: str) -> dict:
        """Acknowledge revocation of the inert demonstration grant."""
        self.grant_active = False
        return {"status": "revoked", "grant_id": grant_id}

    def create_conversation(self, model: str, workspace: Path, active_skills: list[str]) -> dict:
        """Add a local fixture session without creating service-owned records."""
        item = {"conversation_id": "new-" + str(len(self.items)), "title": "New session", "model": model,
                "workspace": str(workspace), "active_skills": active_skills, "draft": "", "pinned": False, "archived": False}
        self.items.insert(0, item)
        return item

    def delete_conversation(self, owner: str) -> dict:
        """Delete only one ephemeral fixture item."""
        self.items = [item for item in self.items if item["conversation_id"] != owner]
        return {"status": "deleted"}

    def update_conversation(self, owner: str, field: str, value: str) -> dict:
        """Change an inert configured selection for context-reset dialog review."""
        return self.session_state(owner, field, value)

    def select_skills(self, owner: str, identifiers: list[str], confirmed: bool) -> dict:
        """Validate demonstration skill IDs without granting or running tools."""
        if set(identifiers) - {"review"}:
            raise DesktopApiError("Only the fixture review skill is installed.")
        return self.session_state(owner, "active_skills", identifiers)

    def cancel_turn(self, owner: str, turn_id: str) -> dict:
        """Acknowledge cancellation without invoking a runtime or model."""
        return {"status": "cancellation_requested"}

    def stream_turn(self, owner: str, message: str, on_event: Callable[[dict], None]) -> None:
        """Deliver a tiny inert response for composer interaction review."""
        for sequence, kind, fields in ((1, "turn_started", {}), (2, "assistant_text", {"text": "Fixture response · no model or tools were called."}),
                                       (3, "completion", {"outcome": "success"})):
            on_event({"conversation_id": owner, "turn_id": "fixture-" + message[:20], "sequence": sequence, "type": kind, **fields})

    def resolve_permission(self, request_id: str, decision: str) -> dict:
        """Acknowledge an inert fixture choice without granting actual access."""
        return {"status": "fixture"}


def build_fixture(root: tk.Tk, state: str) -> FluxDesktop:
    """Build one representative screen using the production components and inert events."""
    client = FixtureClient()
    def task(desktop: FluxDesktop, work: Callable[[], object], complete: Callable[[object], None] | None = None, failed: Callable[[Exception], None] | None = None) -> None:
        """Deliver deterministic fixture requests on the Tk thread."""
        result = work()
        if complete:
            complete(result)
    with patch.object(FluxDesktop, "_run_task", task), patch.object(FluxDesktop, "_load_history"):
        desktop = FluxDesktop(root, client, lambda: None, Path.cwd())
        desktop._select_conversation(client.items[0])
        view = desktop.views["session"]
        view.recover([])
        if state != "empty":
            view.pending_message = "Review this implementation and explain the next steps."
            def event(sequence: int, kind: str, **fields: object) -> None:
                """Deliver one stable public fixture event without triggering tools."""
                view.accept({"conversation_id": "session", "turn_id": "turn", "sequence": sequence, "type": kind, **fields})
            event(1, "turn_started")
            event(2, "tool_call", name="read_file", tool_call_id="read")
            event(3, "tool_result", name="read_file", tool_call_id="read", result={"ok": True, "complete": False, "truncated": True, "next_line": 81, "lines_read": 80, "content": "PRIVATE PAYLOAD MUST NEVER DISPLAY"})
            text = "## Implementation review\nThe service keeps **permissions** and persistence separate from presentation.\n\n- Preserve ordered events\n- Keep keyboard focus visible\n\n| Component | State |\n| --- | --- |\n| Sessions | Ready |\n| Runtime | Connected |\n\n```python\ndef describe_change():\n    return 'Keep the seam small'\n```\n"
            if state == "long":
                text += "\n```python\n" + "print('A long code block with copy and safe expansion')\n" * 160 + "```\n" + "Long displayed explanation. " * 250
            event(4, "assistant_text", text=text)
            if state == "error":
                event(5, "failure", message="Connection interrupted. Reconnect to recover retained results.")
                event(6, "completion", outcome="model_error")
                view.error = "The local backend is unavailable. Reconnect when it is ready."
            elif state not in {"active", "permission"}:
                event(5, "completion", outcome="success")
            if state == "working":
                view.expanded_working.add("turn")
                view.expanded_operations.add(("turn", 2))
        desktop._render_conversation()
        if state == "permission":
            desktop._show_permission("session", {"conversation_id": "session", "turn_id": "turn", "type": "permission_request", "permission_request_id": "fixture",
                 "choices": ["allow_once", "allow_session", "allow_durable", "deny"], "operation": "Apply file changes", "category": "write",
                 "scope": "File · C:/Projects/sample/app.py", "workspace": "C:/Projects/sample", "summary": "1 file change · overwrite: 1"})
        elif state == "settings":
            desktop._settings_loaded((client.settings(), client.list_grants()))
        elif state == "skills":
            desktop.open_skills()
        elif state == "sessions":
            desktop.search_text.set("implementation")
            desktop._replace_conversations(client.list_conversations(search="implementation"), select=False)
        return desktop


def main() -> None:
    """Open a requested manual-review fixture at a representative size and scaling."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", choices=STATES, default="empty")
    parser.add_argument("--size", default="1120x740")
    parser.add_argument("--scale", type=float, default=1.0)
    arguments = parser.parse_args()
    root = tk.Tk()
    root.tk.call("tk", "scaling", (96 / 72) * arguments.scale)
    desktop = build_fixture(root, arguments.state)
    width, height = map(int, arguments.size.split("x"))
    desktop.window_frame.restore_geometry({"width": width, "height": height, "x": 80, "y": 50}, display_bounds(80, 50))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
