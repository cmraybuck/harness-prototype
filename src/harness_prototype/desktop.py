"""Native desktop shell for the loopback FLUX application service."""

from collections.abc import Callable
import http.client
import json
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .application_service import DesktopServiceHost
from .application_state import StateStoreError


REQUEST_TIMEOUT_SECONDS = 5
DISPLAY_TEXT_LIMIT = 12000
STREAM_EVENT_LIMIT = 2 * 1024 * 1024

PERMISSION_CHOICES = {
    "allow_once": "Allow once · This request only",
    "allow_session": "Allow for application session · This scope until FLUX exits",
    "allow_durable": "Allow durably · This scope until revoked in Settings",
    "deny": "Deny · This request (directory denial remembered in this conversation)",
}


class DesktopApiError(RuntimeError):
    """Represent a recoverable failure while calling the private backend."""

    def __init__(self, message: str, code: str = "backend_unavailable") -> None:
        """Store a safe display message and stable error code."""
        super().__init__(message)
        self.code = code


class DesktopApiClient:
    """Call the versioned loopback API without sharing backend internals."""

    def __init__(self, address: tuple[str, int]) -> None:
        """Create a client for one managed loopback service address."""
        self.base_url = f"http://{address[0]}:{address[1]}/v1"

    def health(self) -> dict[str, Any]:
        """Return backend and Ollama readiness information."""
        return self._request("GET", "/health")

    def capabilities(self) -> dict[str, Any]:
        """Return configured models and portable backend capabilities."""
        return self._request("GET", "/capabilities")

    def resolve_permission(self, request_id: str, decision: str) -> dict[str, Any]:
        """Resolve exactly one temporary backend permission request."""
        return self._request("POST", f"/permissions/{quote(request_id, safe='')}", {"decision": decision})

    def settings(self) -> dict[str, Any]:
        """Read credential-free local model and backend preferences."""
        return self._request("GET", "/settings")

    def save_settings(self, model: str, host: str) -> dict[str, Any]:
        """Save validated preferences; backend changes apply after restart."""
        return self._request("PATCH", "/settings", {"default_model": model, "backend_host": host})

    def list_grants(self) -> list[dict[str, Any]]:
        """Inspect durable permission scopes through their backend owner."""
        return self._request("GET", "/grants")["grants"]

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        """Revoke one durable grant without widening or changing other grants."""
        return self._request("DELETE", f"/grants/{quote(grant_id, safe='')}")

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return the first bounded conversation page for navigation."""
        return self._request("GET", "/conversations?limit=100")["conversations"]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return current metadata for one conversation."""
        return self._request("GET", f"/conversations/{quote(conversation_id, safe='')}")

    def skills(self, workspace: Path, conversation_id: str | None = None) -> dict[str, Any]:
        """Discover metadata or inspect the exact active conversation selection."""
        path = (f"/conversations/{quote(conversation_id, safe='')}/skills" if conversation_id
                else "/skills?" + urlencode({"workspace": str(workspace)}))
        return self._request("GET", path)

    def select_skills(self, conversation_id: str, identifiers: list[str], confirmed: bool) -> dict[str, Any]:
        """Apply dynamic activation, optionally requesting an explicit context reset."""
        return self._request("PATCH", f"/conversations/{quote(conversation_id, safe='')}/skills",
                             {"active_skills": identifiers, "confirm_context_reset": confirmed})

    def save_skill_preferences(self, workspace: Path, identifiers: list[str]) -> dict[str, Any]:
        """Persist workspace defaults as identifiers rather than instruction contents."""
        return self._request("PATCH", "/skill-preferences", {"workspace": str(workspace), "active_skills": identifiers})

    def create_conversation(self, model: str, workspace: Path, active_skills: list[str] | None = None) -> dict[str, Any]:
        """Create one backend-owned durable conversation."""
        body = {"model": model, "workspace": str(workspace)}
        if active_skills is not None:
            body["active_skills"] = active_skills
        return self._request(
            "POST", "/conversations", body
        )

    def update_conversation(
        self, conversation_id: str, field: str, value: str,
    ) -> dict[str, Any]:
        """Select one model or workspace through the backend contract."""
        if field not in {"model", "workspace"}:
            raise ValueError("field must be model or workspace")
        return self._request(
            "PATCH", f"/conversations/{quote(conversation_id, safe='')}", {field: value}
        )

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        """Read all durable turns through bounded, append-stable pages."""
        turns = []
        cursor = 0
        while True:
            page = self._request(
                "GET", f"/conversations/{quote(conversation_id, safe='')}/history"
                f"?limit=50&after={cursor}",
            )
            turns.extend(page["turns"])
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                return turns
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                raise DesktopApiError("Invalid conversation history cursor.")
            cursor = next_cursor

    def cancel_turn(self, conversation_id: str, turn_id: str) -> dict[str, Any]:
        """Cancel only the identified active turn, never a later replacement."""
        return self._request(
            "POST", f"/conversations/{quote(conversation_id, safe='')}/turns/"
            f"{quote(turn_id, safe='')}/cancel", {},
        )

    def stream_turn(
        self, conversation_id: str, message: str, on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        """Deliver bounded NDJSON events on the calling worker thread."""
        request = Request(
            self.base_url + f"/conversations/{quote(conversation_id, safe='')}/turns",
            data=json.dumps({"message": message}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
        )
        address = urlsplit(request.full_url)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            connection.connect()
            # Model requests and permission waits may legitimately be silent.
            connection.sock.settimeout(None)
            connection.request("POST", address.path, request.data, dict(request.header_items()))
            with connection.getresponse() as response:
                if response.status != 200:
                    details = json.loads(response.read().decode("utf-8"))["error"]
                    raise DesktopApiError(details["message"], details["code"])
                completed = False
                while True:
                    line = response.readline(STREAM_EVENT_LIMIT + 1)
                    if not line:
                        break
                    if len(line) > STREAM_EVENT_LIMIT:
                        raise DesktopApiError("Turn output exceeded the transport limit.")
                    event = json.loads(line.decode("utf-8"))
                    if not isinstance(event, dict):
                        raise DesktopApiError("Invalid turn event.")
                    on_event(event)
                    completed = event.get("type") == "completion"
                if not completed:
                    raise DesktopApiError("Connection interrupted. Reconnect to recover this turn.")
        except (OSError, http.client.HTTPException, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DesktopApiError("Connection interrupted. Reconnect to recover this turn.") from error
        finally:
            connection.close()

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Exchange one bounded JSON object and normalize expected failures."""
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                details = payload["error"]
                raise DesktopApiError(details["message"], details["code"]) from error
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                raise DesktopApiError(f"Backend request failed ({error.code}).") from error
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DesktopApiError(
                "The local backend is unavailable. Restart FLUX and try again."
            ) from error
        if not isinstance(result, dict):
            raise DesktopApiError("The local backend returned an invalid response.")
        return result


class ConversationView:
    """Keep deduplicated presentation events separate for each conversation."""

    def __init__(self) -> None:
        """Start with no active request or retained display events."""
        self.turns: dict[str, dict[str, Any]] = {}
        self.events: dict[tuple[str, int], dict[str, Any]] = {}
        self.active_turn_id: str | None = None
        self.sending = False
        self.transport_active = False
        self.pending_message: str | None = None
        self.error: str | None = None
        self.history_loaded = False

    def accept(self, event: dict[str, Any]) -> bool:
        """Apply one identified event once, including terminal recovery state."""
        turn_id = event.get("turn_id")
        sequence = event.get("sequence")
        if not isinstance(turn_id, str) or type(sequence) is not int or sequence < 1:
            raise DesktopApiError("Invalid turn event identity.")
        key = (turn_id, sequence)
        if key in self.events:
            return False
        self.events[key] = event
        if event["type"] == "turn_started":
            self.active_turn_id = turn_id
            self.turns.setdefault(turn_id, {"user_prompt": self.pending_message or "", "status": "running"})
            self.pending_message = None
        elif event["type"] == "completion":
            self.turns.setdefault(turn_id, {})["status"] = event.get("outcome", "unknown")
            if self.active_turn_id == turn_id:
                self.active_turn_id = None
                self.sending = False
        return True

    def recover(self, turns: list[dict[str, Any]]) -> None:
        """Merge durable snapshots without replacing newer streamed state."""
        for turn in turns:
            turn_id = turn["turn_id"]
            previous = self.turns.get(turn_id, {})
            terminal_seen = any(event["type"] == "completion" for (saved_id, _), event in self.events.items()
                                if saved_id == turn_id)
            self.turns[turn_id] = {**turn, "status": previous.get("status", turn["status"])
                                 if terminal_seen else turn["status"]}
            for event in turn.get("public_events", []):
                self.accept(event)
            for stored in turn["events"]:
                event = stored.get("public_event")
                if event is not None:
                    self.accept(event)
            if turn["status"] == "running" and not any(
                event["type"] == "completion" for (saved_id, _), event in self.events.items()
                if saved_id == turn_id
            ):
                self.active_turn_id = turn_id
            elif self.active_turn_id == turn_id:
                self.active_turn_id = None
        self.sending = self.transport_active or self.active_turn_id is not None
        if self.pending_message and any(turn.get("user_prompt") == self.pending_message for turn in turns):
            self.pending_message = None
        self.error = None
        self.history_loaded = True

    def render(self) -> list[tuple[str, str]]:
        """Return accessible labelled text without raw tool or permission payloads."""
        rows = []
        for turn_id, turn in self.turns.items():
            rows.append(("user", f"You\n{turn.get('user_prompt', '')}\n\n"))
            events = sorted(
                (event for (saved_id, _), event in self.events.items() if saved_id == turn_id),
                key=lambda event: event["sequence"],
            )
            previous = None
            for event in events:
                kind = event["type"]
                if kind == "turn_started":
                    rows.append(("status", "Working · Thinking…\n"))
                elif kind == "assistant_text":
                    text = str(event.get("text", ""))
                    rows.append(("assistant", ("\nFLUX\n" if previous != kind else "") + text))
                elif kind == "tool_call":
                    rows.append(("status", f"\nWorking · {event.get('name', 'tool')}\n"))
                elif kind == "tool_result":
                    result = event.get("result")
                    result = result if isinstance(result, dict) else {}
                    summary = {
                        key: result[key] for key in (
                            "ok", "complete", "truncated", "next_offset", "next_start_line",
                            "next_line", "total_matches", "returned_count", "exit_code",
                            "lines_read", "start_line", "end_line",
                        ) if key in result and (result[key] is None or type(result[key]) in {int, bool})
                    }
                    for key in ("entries", "matches", "files", "errors"):
                        if isinstance(result.get(key), list):
                            summary[f"{key}_count"] = len(result[key])
                    error = result.get("error")
                    if isinstance(error, dict):
                        summary["error"] = "Tool failed"
                    elif error:
                        summary["error"] = "Tool failed"
                    rows.append(("code", f"Result · {event.get('name', 'tool')}\n"
                                 + json.dumps(summary, ensure_ascii=False, indent=2)[:DISPLAY_TEXT_LIMIT] + "\n"))
                elif kind == "permission_request":
                    pass
                elif kind == "permission_receipt":
                    decision = event.get("decision", "deny")
                    label = PERMISSION_CHOICES.get(decision, "Denied")
                    scope = str(event.get("scope", "Requested scope"))
                    scope = scope if len(scope) <= 300 else scope[:297] + "…"
                    rows.append(("status", f"\n    Permission · {label} · {scope}\n"))
                elif kind == "permission_grant" and event.get("action") == "reused":
                    rows.append(("status", f"\n    Permission · Allowed · {event.get('lifetime')} grant reused\n"))
                elif kind in {"failure", "interruption"}:
                    rows.append(("status", f"\n{kind.title()} · {event.get('message') or 'Turn stopped.'}\n"))
                elif kind == "completion":
                    outcome = event.get("outcome", "unknown")
                    guidance = "" if outcome == "success" else " Send a follow-up to continue with retained results."
                    rows.append(("status", f"\nTurn {outcome}.{guidance}\n\n"))
                previous = kind
            if not events:
                # Older histories retain model responses rather than public events.
                for stored in turn.get("events", []):
                    if stored.get("type") == "model_response" and stored.get("content"):
                        rows.append(("assistant", "FLUX\n" + str(stored["content"]) + "\n"))
            if turn.get("status") != "running" and not any(event["type"] == "completion" for event in events):
                rows.append(("status", f"Turn {turn.get('status')}. Send a follow-up to continue.\n"))
        if self.pending_message:
            rows.append(("user", f"You\n{self.pending_message}\n\n"))
        if self.error:
            rows.append(("status", f"\n{self.error}\n"))
        formatted = []
        in_code = False
        combined: list[tuple[str, str]] = []
        for tag, text in rows:
            if tag == "assistant" and combined and combined[-1][0] == tag:
                combined[-1] = (tag, combined[-1][1] + text)
            else:
                combined.append((tag, text))
        for tag, text in combined:
            if tag != "assistant":
                formatted.append((tag, text))
                in_code = False
                continue
            for line in text.splitlines(keepends=True):
                if line.strip().startswith("```"):
                    in_code = not in_code
                formatted.append(("code" if in_code else tag, line))
        return formatted


class PermissionView:
    """Present a temporary redacted decision without changing the transcript."""

    def __init__(self, root: tk.Tk, event: dict[str, Any], decide: Callable[[str], None]) -> None:
        """Show only backend-owned summary fields and supported scoped choices."""
        self.decide = decide
        self.closed = False
        self.choices = [choice for choice in ("allow_once", "allow_session", "allow_durable", "deny")
                        if choice in event.get("choices", [])]
        oversized = any(len(str(event.get(key, ""))) > DISPLAY_TEXT_LIMIT
                        for key in ("operation", "category", "scope", "workspace", "summary"))
        if oversized:
            self.choices = ["deny"]
        if "deny" not in self.choices:
            self.choices.append("deny")
        self.window = tk.Toplevel(root)
        self.window.title("FLUX · Permission")
        self.window.transient(root)
        self.window.protocol("WM_DELETE_WINDOW", lambda: self.confirm("deny"))
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        summary = "\n".join(f"{label}: {str(event.get(key, fallback))[:DISPLAY_TEXT_LIMIT]}" for label, key, fallback in (
            ("Operation", "operation", event.get("tool_name", "Access directory")),
            ("Risk", "category", "filesystem"), ("Scope", "scope", event.get("path", "Requested operation")),
            ("Workspace", "workspace", "Current workspace"),
            ("Request", "summary", "Directory access includes its subtree."),
        ))
        review = ttk.Frame(frame)
        review.pack(fill=tk.BOTH, expand=True)
        summary_text = tk.Text(review, width=85, height=10, wrap="word", foreground="#087f8c")
        summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        summary_text.insert("1.0", summary)
        summary_text.configure(state="disabled")
        scrollbar = ttk.Scrollbar(review, command=summary_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        summary_text.configure(yscrollcommand=scrollbar.set)
        if oversized:
            ttk.Label(frame, text="Scope exceeds the review limit. Deny and request a smaller operation.").pack(anchor="w")
        self.options = tk.Listbox(frame, width=85, height=len(self.choices), exportselection=False,
                                  selectforeground="#166534", selectbackground="#dcfce7")
        self.options.pack(fill=tk.X, pady=12)
        for choice in self.choices:
            self.options.insert(tk.END, PERMISSION_CHOICES[choice])
        self.options.selection_set(0)
        self.options.activate(0)
        self.options.bind("<Up>", lambda _event: self.move(-1))
        self.options.bind("<Down>", lambda _event: self.move(1))
        self.window.bind("<Return>", lambda _event: self.choose_option())
        self.window.bind("<space>", lambda _event: self.choose_option())
        self.window.bind("<Escape>", lambda _event: self.confirm("deny"))
        ttk.Label(frame, text="Up/Down to select · Space or Enter to confirm · Escape to deny").pack()
        ttk.Button(frame, text="Confirm selection", command=self.choose_option).pack(pady=8)
        self.options.focus_set()

    def move(self, step: int) -> str:
        """Move the focused selection without approving it."""
        current = self.options.curselection()
        index = max(0, min(len(self.choices) - 1, (current[0] if current else 0) + step))
        self.options.selection_clear(0, tk.END)
        self.options.selection_set(index)
        self.options.activate(index)
        return "break"

    def choose_option(self) -> str:
        """Confirm the focused option once through the shared dialog seam."""
        selection = self.options.curselection()
        return self.confirm(self.choices[selection[0]] if selection else "deny")

    def confirm(self, decision: str) -> str:
        """Restore the conversation before asynchronously delivering a decision."""
        if not self.closed:
            self.dismiss()
            self.decide(decision)
        return "break"

    def dismiss(self) -> None:
        """Close an expired decision view without submitting another answer."""
        if not self.closed:
            self.closed = True
            self.window.destroy()


class SkillPanel:
    """Inspect and toggle conversation skills inline without a modal decision."""

    def __init__(
        self, parent: ttk.Frame, catalog: dict[str, Any],
        activate: Callable[[list[str]], None], close: Callable[[], None],
    ) -> None:
        """Build labelled metadata controls with direct keyboard activation."""
        self.activate = activate
        self.selected = set(catalog.get("active_skills", []))
        self.entries = list(catalog.get("skills", []))
        known = {item["id"] for item in self.entries}
        for identifier in sorted(self.selected - known):
            self.entries.append({"id": identifier, "name": identifier, "available": False,
                                 "error": "Saved skill is missing or invalid. Disable it or restore its source."})
        for diagnostic in catalog.get("diagnostics", []):
            self.entries.append({"name": "Invalid skill", "available": False,
                                 "manifest_path": diagnostic["path"], "error": diagnostic["error"]})
        self.frame = ttk.Frame(parent, padding=8)
        self.frame.columnconfigure(1, weight=1)
        ttk.Label(self.frame, text="Skills · Up/Down to inspect · Space or Enter to toggle").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.options = tk.Listbox(self.frame, width=25, height=5, exportselection=False, takefocus=True)
        self.options.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        for item in self.entries:
            status = "Active" if item.get("id") in self.selected else "Inactive"
            if not item.get("available"):
                status += " · Unavailable"
            self.options.insert(tk.END, f"{status} · {item.get('id', item['name'])}")
        self.options.bind("<<ListboxSelect>>", self.inspect)
        self.options.bind("<Up>", lambda _event: self.move(-1))
        self.options.bind("<Down>", lambda _event: self.move(1))
        self.options.bind("<space>", lambda _event: self.choose_option())
        self.options.bind("<Return>", lambda _event: self.choose_option())
        review = ttk.Frame(self.frame)
        review.grid(row=1, column=1, sticky="nsew")
        self.details = tk.Text(review, width=45, height=5, wrap="word", takefocus=True)
        self.details.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(review, command=self.details.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.details.configure(yscrollcommand=scrollbar.set)
        self.notice = tk.StringVar(value=(catalog.get("skill_error") or {}).get("error", ""))
        ttk.Label(self.frame, textvariable=self.notice, wraplength=650).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(self.frame, text="Close skills", command=close).grid(row=3, column=0, sticky="w")
        self.limits = catalog.get("limits", {})
        self.roots = catalog.get("roots", [])
        self.provenance = catalog.get("skill_provenance", [])
        if self.entries:
            self.options.selection_set(0)
        self.inspect()

    def move(self, step: int) -> str:
        """Move the metadata selection without changing active skills."""
        current = self.options.curselection()
        index = max(0, min(len(self.entries) - 1, (current[0] if current else 0) + step))
        self.options.selection_clear(0, tk.END)
        if self.entries:
            self.options.selection_set(index)
            self.options.activate(index)
            self.options.see(index)
        self.inspect()
        return "break"

    def inspect(self, _event: object = None) -> None:
        """Display bounded source metadata, requirements, diagnostics, and loading limits."""
        selection = self.options.curselection()
        item = self.entries[selection[0]] if selection else {}
        text = "\n".join(f"{label}: {item.get(key, 'Not available')}" for label, key in (
            ("Name", "name"), ("Description", "description"), ("Source", "source"),
            ("Manifest location", "manifest_path"), ("Format version", "format_version"),
            ("Required capabilities", "required_capabilities"), ("Required tools", "required_tools"),
            ("Validation state", "validation_state"), ("Explanation", "error"),
        )) if item else "No installed skills. Install a local skill and run /skills again."
        text += "\nLoading limits: " + str(self.limits) + "\nDiscovery roots: " + "; ".join(self.roots)
        for record in self.provenance:
            if record["id"] == item.get("id"):
                text += f"\nSaved active source: {record['manifest_path']}\nContent fingerprint: {record['sha256']}"
        self.details.configure(state="normal")
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def choose_option(self) -> str:
        """Request an immediate atomic toggle without granting tools or permissions."""
        selection = self.options.curselection()
        if selection:
            item = self.entries[selection[0]]
            identifier = item.get("id")
            identifiers = set(self.selected)
            if identifier in identifiers:
                identifiers.remove(identifier)
            elif item.get("available"):
                identifiers.add(identifier)
            else:
                self.notice.set(item.get("error", "This skill is unavailable."))
                return "break"
            self.activate(sorted(identifiers))
        return "break"


class FluxDesktop:
    """Render a responsive native shell over the private backend API."""

    def __init__(
        self,
        root: tk.Tk,
        client: DesktopApiClient,
        stop_backend: Callable[[], None],
        initial_workspace: Path,
    ) -> None:
        """Build application navigation and schedule initial backend loading."""
        self.root = root
        self.client = client
        self.stop_backend = stop_backend
        self.workspace = initial_workspace.resolve()
        self.models: list[str] = []
        self.conversations: list[dict[str, Any]] = []
        self.active_conversation_id: str | None = None
        self.views: dict[str, ConversationView] = {}
        self._history_loading: set[str] = set()
        self._closing = False
        self._permissions: dict[str, tuple[str, str, PermissionView]] = {}
        self._decided_permissions: set[str] = set()
        self._backend_stopped = Event()
        self._skill_panel: SkillPanel | None = None
        self._skill_changing: set[str] = set()
        self._skill_panel_request: object | None = None
        self._skill_metadata: dict[str, dict[str, Any]] = {}
        self._skill_requests: dict[str, object] = {}
        self._ui_callbacks: SimpleQueue[Callable[[], None]] = SimpleQueue()

        self.status_text = tk.StringVar(value="Starting local backend…")
        self.detail_text = tk.StringVar(value="Connecting to FLUX")
        self.model_text = tk.StringVar()
        self.workspace_text = tk.StringVar(value=str(self.workspace))
        self.skills_text = tk.StringVar(value="Active skills · None")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(25, self._drain_ui_callbacks)
        self._run_task(self._load_application, self._application_loaded)

    def _build(self) -> None:
        """Construct the shared component and styling conventions."""
        self.root.title("FLUX")
        self.root.geometry("1080x700")
        self.root.minsize(820, 520)
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#64748b")
        style.configure("Status.TLabel", padding=(10, 6))

        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(1, weight=1)

        ttk.Label(shell, text="FLUX", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(shell, textvariable=self.status_text, style="Status.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        navigation = ttk.Frame(shell, padding=(0, 16, 16, 0), width=260)
        navigation.grid(row=1, column=0, sticky="nsew")
        navigation.grid_propagate(False)
        navigation.rowconfigure(2, weight=1)
        navigation.columnconfigure(0, weight=1)
        ttk.Button(navigation, text="New conversation", command=self.create_conversation).grid(
            row=0, column=0, sticky="ew", pady=(0, 12)
        )
        ttk.Label(navigation, text="Conversations").grid(row=1, column=0, sticky="w")
        self.conversation_list = tk.Listbox(
            navigation, activestyle="none", exportselection=False, borderwidth=0,
            highlightthickness=1, selectmode=tk.SINGLE,
        )
        self.conversation_list.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        self.conversation_list.bind("<<ListboxSelect>>", self._conversation_selected)
        ttk.Button(navigation, text="Settings and durable grants", command=self.open_settings).grid(
            row=3, column=0, sticky="ew", pady=12
        )

        workspace = ttk.Frame(shell, padding=(24, 20))
        workspace.grid(row=1, column=1, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(2, weight=1)
        ttk.Label(workspace, textvariable=self.detail_text, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        controls = ttk.Frame(workspace)
        controls.grid(row=1, column=0, sticky="ew", pady=(18, 10))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Model").grid(row=0, column=0, sticky="w")
        self.model_selector = ttk.Combobox(
            controls, textvariable=self.model_text, state="readonly", width=30
        )
        self.model_selector.grid(row=0, column=1, sticky="ew", padx=(10, 20))
        self.model_selector.bind("<<ComboboxSelected>>", self._model_selected)
        self.workspace_button = ttk.Button(controls, text="Choose workspace…", command=self.choose_workspace)
        self.workspace_button.grid(row=0, column=2)
        ttk.Label(controls, textvariable=self.workspace_text, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Label(controls, textvariable=self.skills_text, wraplength=500).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(controls, text="Skills…", command=self.open_skills).grid(row=2, column=2)

        self._skill_container = controls

        transcript = ttk.Frame(workspace)
        transcript.grid(row=2, column=0, sticky="nsew")
        transcript.columnconfigure(0, weight=1)
        transcript.rowconfigure(0, weight=1)
        self.transcript = tk.Text(transcript, wrap="word", state="disabled", takefocus=True)
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(transcript, command=self.transcript.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("user", foreground="#087f8c")
        self.transcript.tag_configure("assistant", foreground="#166534")
        self.transcript.tag_configure("status", foreground="#475569")
        self.transcript.tag_configure("code", font=("Consolas", 10))
        composer = ttk.Frame(workspace)
        composer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        ttk.Label(composer, text="Message or /skills, /skill id[,id] (Ctrl+Enter to send)").grid(row=0, column=0, sticky="w")
        self.message_input = tk.Text(composer, height=4, wrap="word")
        self.message_input.grid(row=1, column=0, sticky="ew", rowspan=3, padx=(0, 10))
        self.message_input.bind("<Control-Return>", self._send_key)
        self.send_button = ttk.Button(composer, text="Send", command=self.send_turn)
        self.send_button.grid(row=1, column=1, sticky="ew")
        self.cancel_button = ttk.Button(composer, text="Cancel turn", command=self.cancel_turn)
        self.cancel_button.grid(row=2, column=1, sticky="ew")
        self.retry_turn_button = ttk.Button(composer, text="Retry failed turn", command=self.retry_turn)
        self.retry_turn_button.grid(row=3, column=1, sticky="ew")
        self.retry_button = ttk.Button(workspace, text="Reconnect", command=self.refresh)
        self._render_conversation()

    def _run_task(
        self, work: Callable[[], Any], complete: Callable[[Any], None] | None = None,
        failed: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run backend I/O off the Tk event loop and marshal its result safely."""
        def runner() -> None:
            """Execute one operation and queue success or failure on the UI thread."""
            try:
                result = work()
            except (DesktopApiError, OSError, ValueError) as error:
                if not self._closing:
                    self._ui_callbacks.put(lambda error=error: (failed or self._show_error)(error))
            else:
                if complete is not None and not self._closing:
                    self._ui_callbacks.put(lambda: complete(result))

        Thread(target=runner, name="flux-desktop-request", daemon=True).start()

    def _drain_ui_callbacks(self) -> None:
        """Run queued worker completions only from Tk's owning thread."""
        try:
            for _ in range(100):
                self._ui_callbacks.get_nowait()()
        except Empty:
            pass
        if not self._closing:
            self.root.after(25, self._drain_ui_callbacks)

    def _load_application(self) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Fetch independent startup resources from the managed backend."""
        return self.client.health(), self.client.capabilities(), self.client.list_conversations()

    def _application_loaded(
        self, result: tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]],
    ) -> None:
        """Populate controls and render ready, empty, or Ollama recovery state."""
        health, capabilities, conversations = result
        self.models = list(capabilities.get("models", []))
        self.model_selector.configure(values=self.models)
        self._replace_conversations(conversations)
        ollama = health.get("ollama", {})
        if ollama.get("status") == "available":
            self.status_text.set("Backend connected · Ollama available")
            self.retry_button.grid_remove()
        else:
            self.status_text.set("Backend connected · Ollama unavailable")
            self.detail_text.set(ollama.get("message", "Start Ollama, then retry."))
            self.retry_button.grid(row=4, column=0, sticky="w", pady=8)
        if not self.models:
            self.detail_text.set("Configure HARNESS_MODELS, then restart FLUX")
        elif not conversations and ollama.get("status") == "available":
            self.detail_text.set("Start a new conversation")

    def _replace_conversations(self, conversations: list[dict[str, Any]]) -> None:
        """Replace navigation items while keeping backend identifiers out of labels."""
        self.conversations = conversations
        self.conversation_list.delete(0, tk.END)
        for conversation in conversations:
            self.conversation_list.insert(tk.END, conversation.get("title", "New conversation"))
        if conversations:
            index = next((index for index, item in enumerate(conversations)
                          if item["conversation_id"] == self.active_conversation_id), 0)
            self.conversation_list.selection_set(index)
            self._select_conversation(conversations[index])

    def _conversation_selected(self, _event: object) -> None:
        """Load the selected navigation item's backend metadata."""
        selection = self.conversation_list.curselection()
        if not selection:
            return
        conversation_id = self.conversations[selection[0]]["conversation_id"]
        self._select_conversation(self.conversations[selection[0]])

    def _select_conversation(self, conversation: dict[str, Any]) -> None:
        """Display one conversation's backend-owned selections."""
        if self.active_conversation_id != conversation["conversation_id"]:
            self.close_skills()
        self.active_conversation_id = conversation["conversation_id"]
        self.detail_text.set(conversation.get("title", "New conversation"))
        self.model_text.set(conversation.get("model", ""))
        self.workspace_text.set(conversation.get("workspace", str(self.workspace)))
        self._skill_metadata[self.active_conversation_id] = conversation
        self._render_skills()
        self.views.setdefault(self.active_conversation_id, ConversationView())
        self._render_conversation()
        self._load_history(self.active_conversation_id)
        conversation_id = self.active_conversation_id
        request_token = object()
        self._skill_requests[conversation_id] = request_token

        def loaded(metadata: dict[str, Any]) -> None:
            """Refresh restart diagnostics without changing the selected conversation."""
            if self._skill_requests.get(conversation_id) is not request_token:
                return
            self._skill_metadata[conversation_id] = metadata
            if self.active_conversation_id == conversation_id:
                self._render_skills()

        self._run_task(lambda: self.client.get_conversation(conversation_id), loaded)

    def _load_history(self, conversation_id: str) -> None:
        """Recover durable events without blocking or duplicating a stream."""
        if conversation_id in self._history_loading or self._closing:
            return
        self._history_loading.add(conversation_id)

        def worker() -> None:
            """Read history off-thread and deliver a conversation-scoped snapshot."""
            try:
                turns = self.client.history(conversation_id)
            except (DesktopApiError, OSError, ValueError) as error:
                self._ui_callbacks.put(lambda error=error: finished(None, error))
            else:
                self._ui_callbacks.put(lambda: finished(turns, None))

        def finished(turns: list[dict[str, Any]] | None, error: Exception | None) -> None:
            """Merge one snapshot and poll only while its turn remains active."""
            self._history_loading.discard(conversation_id)
            view = self.views[conversation_id]
            if error:
                view.error = str(error)
                self._dismiss_permissions(conversation_id, deny=True)
            else:
                view.recover(turns or [])
                resolved = {event.get("permission_request_id") for event in view.events.values()
                            if event["type"] == "permission_receipt"}
                for request_id in resolved:
                    pending = self._permissions.pop(request_id, None)
                    if pending:
                        pending[2].dismiss()
                for event in view.events.values():
                    if (event["type"] == "permission_request" and event["turn_id"] == view.active_turn_id
                            and event.get("permission_request_id") not in resolved):
                        self._show_permission(conversation_id, event)
                if not view.active_turn_id:
                    self._dismiss_permissions(conversation_id)
            if self.active_conversation_id == conversation_id:
                self._render_conversation()
            if view.active_turn_id and not self._closing:
                self.root.after(1000, lambda: self._load_history(conversation_id))

        Thread(target=worker, name="flux-desktop-history", daemon=True).start()

    def _render_conversation(self) -> None:
        """Render the selected view and expose only valid turn actions."""
        view = self.views.get(self.active_conversation_id)
        bottom = self.transcript.yview()[1] >= 0.99
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", tk.END)
        for tag, text in view.render() if view else [("status", "Select or create a conversation to begin.")]:
            self.transcript.insert(tk.END, text, tag)
        self.transcript.configure(state="disabled")
        if bottom:
            self.transcript.see(tk.END)
        busy = self.active_conversation_id in self._skill_changing or bool(view and (not view.history_loaded or view.sending or view.transport_active or view.active_turn_id))
        self.send_button.configure(state="normal" if view and not busy and not view.error else "disabled")
        self.cancel_button.configure(state="normal" if view and view.active_turn_id else "disabled")
        self.retry_turn_button.configure(state="normal" if view and self._retry_message(view) else "disabled")
        if view and view.error:
            self.retry_button.grid(row=4, column=0, sticky="w", pady=8)
        self.model_selector.configure(state="disabled" if busy else "readonly")
        self.workspace_button.configure(state="disabled" if busy else "normal")

    def _send_key(self, _event: object) -> str:
        """Submit keyboard input without inserting an extra newline."""
        self.send_turn()
        return "break"

    def send_turn(self, message: str | None = None) -> None:
        """Reserve only the selected conversation and stream on its own worker."""
        conversation_id = self.active_conversation_id
        if not conversation_id:
            return
        view = self.views[conversation_id]
        if conversation_id in self._skill_changing or not view.history_loaded or view.sending or view.transport_active or view.active_turn_id:
            return
        message = message if message is not None else self.message_input.get("1.0", "end-1c")
        if not message.strip():
            return
        command = message.strip()
        normalized = command.lower()
        if normalized == "/skills":
            self.message_input.delete("1.0", tk.END)
            self.open_skills()
            return
        if normalized == "/skill" or normalized.startswith("/skill "):
            identifiers = [item.strip() for item in command[6:].strip().split(",") if item.strip()]
            self.message_input.delete("1.0", tk.END)
            self.activate_skills(conversation_id, identifiers)
            return
        if view.error:
            return
        view.sending = True
        view.transport_active = True
        view.pending_message = message
        self.message_input.delete("1.0", tk.END)
        self._render_conversation()

        def worker() -> None:
            """Queue streamed events in order without touching Tk from a worker."""
            try:
                self.client.stream_turn(
                    conversation_id, message,
                    lambda event: self._ui_callbacks.put(
                        lambda event=event: self._turn_event(conversation_id, event)
                    ),
                )
                self._ui_callbacks.put(lambda: self._stream_finished(conversation_id))
            except (DesktopApiError, OSError, ValueError) as error:
                self._ui_callbacks.put(lambda error=error: self._turn_error(conversation_id, error))

        Thread(target=worker, name="flux-desktop-turn", daemon=True).start()

    def _stream_finished(self, conversation_id: str) -> None:
        """Release the transport reservation after its last queued event."""
        view = self.views[conversation_id]
        view.transport_active = False
        view.sending = view.active_turn_id is not None
        if self.active_conversation_id == conversation_id:
            self._render_conversation()

    def _turn_event(self, conversation_id: str, event: dict[str, Any]) -> None:
        """Route each event to its owning conversation despite navigation changes."""
        if event.get("conversation_id") != conversation_id:
            self._turn_error(conversation_id, DesktopApiError("Mismatched conversation event."))
            return
        try:
            accepted = self.views[conversation_id].accept(event)
        except DesktopApiError as error:
            self._turn_error(conversation_id, error)
            return
        if accepted and event["type"] == "permission_request":
            self._show_permission(conversation_id, event)
        elif event["type"] == "permission_receipt":
            pending = self._permissions.pop(event.get("permission_request_id"), None)
            if pending:
                pending[2].dismiss()
        elif event["type"] in {"completion", "interruption", "failure"}:
            self._dismiss_permissions(conversation_id)
        if self.active_conversation_id == conversation_id:
            self._render_conversation()

    def _turn_error(self, conversation_id: str, error: Exception) -> None:
        """Preserve uncertain submissions and require recovery before another send."""
        view = self.views[conversation_id]
        view.error = str(error)
        view.transport_active = False
        view.sending = False
        self._dismiss_permissions(conversation_id, deny=True)
        if isinstance(error, DesktopApiError) and error.code == "backend_unavailable":
            self.close_skills()
        if self.active_conversation_id == conversation_id:
            self._render_conversation()

    def cancel_turn(self) -> None:
        """Capture both IDs so switching views cannot cancel another turn."""
        conversation_id = self.active_conversation_id
        view = self.views.get(conversation_id)
        if view and view.active_turn_id:
            turn_id = view.active_turn_id
            self._dismiss_permissions(conversation_id, deny=True)
            self._run_task(lambda: self.client.cancel_turn(conversation_id, turn_id))

    def _show_permission(self, conversation_id: str, event: dict[str, Any]) -> None:
        """Keep each temporary decision bound to its original conversation and turn."""
        request_id = event.get("permission_request_id")
        if (not isinstance(request_id, str) or request_id in self._permissions
                or request_id in self._decided_permissions or self._closing):
            return
        view = self.views.get(conversation_id)
        if not view or view.active_turn_id != event.get("turn_id") or view.error:
            return

        def decide(decision: str) -> None:
            """Deliver one choice off-thread, cancelling on a lost backend connection."""
            self._permissions.pop(request_id, None)
            self._decided_permissions.add(request_id)

            def work() -> None:
                """Resolve the captured request without retrying an uncertain approval."""
                try:
                    self.client.resolve_permission(request_id, decision)
                except DesktopApiError as error:
                    if error.code != "permission_not_found":
                        try:
                            self.client.cancel_turn(conversation_id, event["turn_id"])
                        except DesktopApiError:
                            pass
                        raise

            self._run_task(work)

        dialog = PermissionView(self.root, event, decide)
        self._permissions[request_id] = (conversation_id, event["turn_id"], dialog)
        self.root.after(1000, lambda: self._load_history(conversation_id))

    def _dismiss_permissions(self, conversation_id: str | None = None, *, deny: bool = False) -> None:
        """Expire or deny only dialogs belonging to the affected conversation."""
        for request_id, (owner, _, dialog) in list(self._permissions.items()):
            if conversation_id is None or owner == conversation_id:
                if deny:
                    dialog.confirm("deny")
                else:
                    dialog.dismiss()
                self._permissions.pop(request_id, None)

    def open_settings(self) -> None:
        """Load local preferences and durable scopes without blocking Tk."""
        self._run_task(lambda: (self.client.settings(), self.client.list_grants()), self._settings_loaded)

    def _settings_loaded(self, result: tuple[dict[str, Any], list[dict[str, Any]]]) -> None:
        """Build a local settings view with exact, individually revocable grants."""
        settings, grants = result
        window = tk.Toplevel(self.root)
        window.title("FLUX · Local settings")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        model = tk.StringVar(value=settings["default_model"])
        host = tk.StringVar(value=settings["backend_host"])
        ttk.Label(frame, text="Default model for new conversations").pack(anchor="w")
        ttk.Combobox(frame, textvariable=model, values=self.models, state="readonly").pack(fill=tk.X)
        ttk.Label(frame, text="Ollama HTTP(S) address · Applies after restarting FLUX").pack(anchor="w", pady=(12, 0))
        ttk.Entry(frame, textvariable=host, width=70).pack(fill=tk.X)
        notice = tk.StringVar()
        ttk.Label(frame, textvariable=notice, style="Muted.TLabel").pack(anchor="w")

        def saved(_result: dict[str, Any]) -> None:
            """Confirm persistence without touching existing conversations."""
            if window.winfo_exists():
                notice.set("Saved. Restart FLUX to apply the backend address.")

        def save() -> None:
            """Capture Tk values before validating preferences on the backend."""
            selected_model, selected_host = model.get(), host.get().strip()
            self._run_task(lambda: self.client.save_settings(selected_model, selected_host), saved)

        ttk.Button(frame, text="Save preferences", command=save).pack(anchor="w", pady=8)
        ttk.Label(frame, text="Durable grants · Valid until revoked · Restricted to the listed workspace").pack(anchor="w")
        listing = tk.Listbox(frame, width=100, height=10, exportselection=False)
        listing.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=listing.xview)
        scroll.pack(fill=tk.X)
        listing.configure(xscrollcommand=scroll.set)
        for grant in grants:
            scope = grant["target"] if grant["kind"] != "operation" else "Exact operation " + grant["target"].split(":sha256:")[0]
            listing.insert(tk.END, f"{grant['kind']} · {scope} · Workspace: {grant['workspace']} · Until revoked")

        def revoked(_result: dict[str, Any]) -> None:
            """Refresh grant administration after confirmed backend revocation."""
            if window.winfo_exists():
                window.destroy()
                self.open_settings()

        def revoke() -> None:
            """Revoke the selected stable grant identity exactly once."""
            selection = listing.curselection()
            if selection:
                grant_id = grants[selection[0]]["grant_id"]
                self._run_task(lambda: self.client.revoke_grant(grant_id), revoked)

        ttk.Button(frame, text="Revoke selected grant", command=revoke).pack(anchor="w", pady=8)

    def _retry_message(self, view: ConversationView) -> str | None:
        """Allow resubmission only after a failed turn without tool side effects."""
        if view.sending or view.transport_active or view.active_turn_id or view.error or not view.turns:
            return None
        turn_id = next(reversed(view.turns))
        turn = view.turns[turn_id]
        if turn.get("status") not in {"failed", "model_error"}:
            return None
        if any(event["type"] in {"tool_call", "tool_result", "permission_request"}
               for (saved_id, _), event in view.events.items() if saved_id == turn_id):
            return None
        if any(event.get("type") in {"tool_call", "tool_result", "tool_approval"}
               for event in turn.get("events", [])):
            return None
        return turn.get("user_prompt")

    def retry_turn(self) -> None:
        """Resubmit an eligible failed prompt explicitly, never automatically."""
        view = self.views.get(self.active_conversation_id)
        if view:
            message = self._retry_message(view)
            if message:
                self.send_turn(message)

    def create_conversation(self) -> None:
        """Create a normal conversation with no preselected skills or picker."""
        if not self.models:
            self._show_error(DesktopApiError("No Ollama models are configured."))
            return
        workspace = Path(self.workspace_text.get())

        def created(metadata: dict[str, Any]) -> None:
            """Select the new conversation after creation without opening skill controls."""
            self.active_conversation_id = metadata["conversation_id"]
            self.close_skills()
            self.refresh()

        self._run_task(
            lambda: self.client.create_conversation(self.client.settings()["default_model"], workspace, []),
            created,
        )

    def _render_skills(self) -> None:
        """Keep active identifiers and restoration failures visible in the conversation."""
        metadata = self._skill_metadata.get(self.active_conversation_id, {})
        label = "Active skills · " + (", ".join(metadata.get("active_skills", [])) or "None")
        if metadata.get("skill_error"):
            label += " · Unavailable: " + metadata["skill_error"]["error"]
        self.skills_text.set(label)

    def close_skills(self) -> None:
        """Close the inline inspector and restore composer focus."""
        self._skill_panel_request = None
        if self._skill_panel:
            self._skill_panel.frame.destroy()
            self._skill_panel = None
        self.message_input.focus_set()

    def open_skills(self) -> None:
        """Rediscover and inspect the selected conversation's skills inline."""
        conversation_id = self.active_conversation_id
        if not conversation_id:
            self.status_text.set("Create a conversation before using /skills.")
            return
        workspace = Path(self.workspace_text.get())
        request_token = object()
        self._skill_panel_request = request_token

        def loaded(catalog: dict[str, Any]) -> None:
            """Render only the captured conversation and keep navigation responsive."""
            if self.active_conversation_id != conversation_id or self._skill_panel_request is not request_token:
                return
            focused_index = 0
            if self._skill_panel:
                selection = self._skill_panel.options.curselection()
                focused_index = selection[0] if selection else 0
                self._skill_panel.frame.destroy()
            self._skill_metadata[conversation_id] = catalog
            self._render_skills()
            self._skill_panel = SkillPanel(self._skill_container, catalog,
                                          lambda identifiers: self.activate_skills(conversation_id, identifiers),
                                          self.close_skills)
            self._skill_panel.frame.grid(row=3, column=0, columnspan=3, sticky="ew")
            self._skill_panel.move(focused_index)
            self._skill_panel.options.focus_set()

        self._run_task(lambda: self.client.skills(workspace, conversation_id), loaded)

    def activate_skills(self, conversation_id: str, identifiers: list[str]) -> None:
        """Apply slash-command or keyboard activation without discarding retained context."""
        view = self.views[conversation_id]
        if conversation_id in self._skill_changing or view.sending or view.transport_active or view.active_turn_id or not view.history_loaded:
            self.status_text.set("Wait for this conversation's pending operation before changing skills.")
            return
        self._skill_changing.add(conversation_id)
        if self.active_conversation_id == conversation_id:
            self._skill_panel_request = None
        self._render_conversation()

        def applied(metadata: dict[str, Any]) -> None:
            """Update only the captured chat after backend validation and persistence."""
            self._skill_changing.discard(conversation_id)
            self._skill_requests[conversation_id] = object()
            self._skill_metadata[conversation_id] = metadata
            if view.error:
                self._load_history(conversation_id)
            if self.active_conversation_id == conversation_id:
                self._render_skills()
                self._render_conversation()
                self.status_text.set("Skills updated · Conversation context retained")
                if self._skill_panel:
                    self.open_skills()

        def failed(error: Exception) -> None:
            """Keep the prior set and show failures without automatically retrying changes."""
            self._skill_changing.discard(conversation_id)
            if self.active_conversation_id == conversation_id:
                self._render_conversation()
                self._show_error(error)
                if self._skill_panel:
                    self._skill_panel.notice.set(str(error))

        self._run_task(lambda: self.client.select_skills(conversation_id, identifiers, False), applied, failed)

    def choose_workspace(self) -> None:
        """Choose an existing directory and update the active conversation if present."""
        selected = filedialog.askdirectory(initialdir=self.workspace_text.get(), mustexist=True)
        if not selected:
            return
        self.workspace = Path(selected).resolve()
        self.workspace_text.set(str(self.workspace))
        if self.active_conversation_id:
            conversation_id = self.active_conversation_id
            self._run_task(
                lambda: self.client.update_conversation(
                    conversation_id, "workspace", str(self.workspace)
                ),
                self._select_conversation,
            )

    def _model_selected(self, _event: object) -> None:
        """Update the active conversation's model through the backend API."""
        if not self.active_conversation_id:
            return
        conversation_id = self.active_conversation_id
        model = self.model_text.get()
        self._run_task(
            lambda: self.client.update_conversation(conversation_id, "model", model),
            self._select_conversation,
        )

    def refresh(self) -> None:
        """Retry startup resources after a recoverable outage."""
        self.status_text.set("Refreshing connection…")
        self._run_task(self._load_application, self._application_loaded)

    def _show_error(self, error: Exception) -> None:
        """Render a recoverable backend or validation error without blocking the UI."""
        self.status_text.set("Needs attention")
        if isinstance(error, DesktopApiError) and error.code == "backend_unavailable":
            self._dismiss_permissions(deny=True)
            self.close_skills()
        self.detail_text.set(str(error))
        self.retry_button.grid(row=4, column=0, sticky="w", pady=8)

    def close(self) -> None:
        """Close the window promptly and stop the managed backend asynchronously."""
        if self._closing:
            return
        self._dismiss_permissions(deny=True)
        if self._skill_panel:
            self.close_skills()
        self._closing = True
        self.root.withdraw()

        def shutdown() -> None:
            """Release backend resources before destroying the Tk interpreter."""
            try:
                self.stop_backend()
            finally:
                self._backend_stopped.set()

        Thread(target=shutdown, name="flux-desktop-shutdown", daemon=True).start()
        self.root.after(25, self._finish_close)

    def _finish_close(self) -> None:
        """Destroy the window after the managed backend releases its resources."""
        if self._backend_stopped.is_set():
            self.root.destroy()
        else:
            self.root.after(25, self._finish_close)


def desktop_main() -> int:
    """Launch FLUX as an installed native desktop application."""
    workspace = Path.cwd()
    host: DesktopServiceHost | None = None
    try:
        host = DesktopServiceHost(workspace=workspace)
        host.start()
        root = tk.Tk()
    except (OSError, StateStoreError, tk.TclError, ValueError) as error:
        if host is not None:
            host.stop()
        print(f"FLUX desktop startup failed: {error}")
        return 1
    FluxDesktop(root, DesktopApiClient(host.address), host.stop, workspace)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(desktop_main())
