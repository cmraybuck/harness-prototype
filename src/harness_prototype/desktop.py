"""Native desktop shell for the loopback FLUX application service."""

from collections.abc import Callable
import http.client
import json
import re
import sys
import time
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any
from weakref import WeakKeyDictionary
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .application_service import DesktopServiceHost
from .application_state import StateStoreError
from .desktop_design import COLORS, FONT_FAMILY, MOTION, TEXT_SIZES, ContextHint, DialogBody, DropdownButton, DropdownMenu, FluxTheme, SessionList, SessionSearch, draw_mark, markdown_blocks
from .desktop_platform import WindowFrame, display_bounds, open_link, system_reduced_motion, dark_dialog_frame


REQUEST_TIMEOUT_SECONDS = 5
DISPLAY_TEXT_LIMIT = 12000
ZOOM_LEVELS = (75, 90, 100, 110, 125, 150)
STREAM_EVENT_LIMIT = 2 * 1024 * 1024

PERMISSION_CHOICES = {
    "allow_once": "Allow once · This request only",
    "allow_session": "Allow for app session · Until FLUX exits",
    "allow_durable": "Remember this scope · Until revoked in Settings",
    "deny": "Deny · This request",
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

    def presentation(self) -> dict[str, Any]:
        """Read persisted appearance and shell preferences."""
        return self._request("GET", "/presentation")

    def save_presentation(self, values: dict[str, Any]) -> dict[str, Any]:
        """Save only validated appearance and window fields."""
        return self._request("PATCH", "/presentation", values)

    def session_state(self, conversation_id: str, field: str, value: Any) -> dict[str, Any]:
        """Rename, pin, archive, or save a draft through its backend owner."""
        if field not in {"title", "pinned", "archived", "draft"}:
            raise ValueError("Unknown session field.")
        return self._request("PATCH", f"/conversations/{quote(conversation_id, safe='')}", {field: value})

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Permanently delete only the selected idle session's database records."""
        return self._request("DELETE", f"/conversations/{quote(conversation_id, safe='')}")

    def list_conversations(self, *, archived: bool = False, search: str = "") -> list[dict[str, Any]]:
        """Read activity-ordered session pages with an optional cross-archive search."""
        items = []
        cursor = None
        while True:
            query = {"limit": 100, "archived": str(archived).lower()}
            if search:
                query["search"] = search
            if cursor:
                query["after"] = cursor
            page = self._request("GET", "/conversations?" + urlencode(query))
            items.extend(page["conversations"])
            following = page.get("next_cursor")
            if following is None:
                return items
            if not isinstance(following, str) or following == cursor or any(item["conversation_id"] == following for item in items[:-1]):
                raise DesktopApiError("Invalid session cursor. Refresh the session list.")
            cursor = following

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
        self.expanded_working: set[str] = set()
        self.expanded_operations: set[tuple[str, int]] = set()
        self.expanded_content: set[str] = set()
        self.scroll_index = "1.0"

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


class InlineDecision(ttk.Frame):
    """Host a temporary decision inside the chat area with shared theme styling."""

    def __init__(self, root: tk.Tk) -> None:
        """Overlay the chat while preserving its widgets, draft, and prior focus."""
        super().__init__(root, style="Surface.TFrame", padding=8)
        self.previous_focus = root.focus_get()
        parent = getattr(root, "_flux_decision_parent", root)
        self.place(in_=parent, relx=0.5, rely=0.5, anchor="center", relwidth=0.94, relheight=0.9)
        self.lift()

    def activate(self) -> None:
        """Route descendant keyboard events through the decision's own bindings."""
        pending = list(self.winfo_children())
        while pending:
            widget = pending.pop()
            tags = widget.bindtags()
            widget.bindtags((tags[0], str(self), *tags[1:]))
            pending.extend(widget.winfo_children())

    def destroy(self) -> None:
        """Restore focus after removing only the temporary decision widgets."""
        super().destroy()
        if self.previous_focus is not None and self.previous_focus.winfo_exists():
            self.previous_focus.focus_set()


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
        self.window = InlineDecision(root)
        heading = ttk.Frame(self.window, padding=(20, 12))
        heading.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(heading, text="Deny and close", command=lambda: self.confirm("deny")).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Label(heading, text="Session · " + str(event.get("session_title", "Current session")), style="Title.TLabel", wraplength=560).pack(anchor="w")
        footer = ttk.Frame(self.window, padding=(16, 8))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        self.body = DialogBody(self.window)
        frame = self.body.frame
        summary = "\n".join(f"{label}: {str(event.get(key, fallback))[:DISPLAY_TEXT_LIMIT]}" for label, key, fallback in (
            ("Operation", "operation", event.get("tool_name", "Access directory")),
            ("Risk", "category", "filesystem"), ("Scope", "scope", event.get("path", "Requested operation")),
            ("Workspace", "workspace", "Current workspace"),
            ("Request", "summary", "Directory access includes its subtree."),
        ))
        review = ttk.Frame(frame)
        review.pack(fill=tk.BOTH, expand=True)
        summary_text = tk.Text(review, width=70, height=5, wrap="word", background=COLORS["surface"], foreground=COLORS["accent"], font="TkDefaultFont", highlightthickness=0)
        summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        summary_text.insert("1.0", summary)
        summary_text.configure(state="disabled")
        scrollbar = ttk.Scrollbar(review, command=summary_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        summary_text.configure(yscrollcommand=scrollbar.set)
        if oversized:
            ttk.Label(frame, text="Scope exceeds the review limit. Deny and request a smaller operation.").pack(anchor="w")
        self.options = tk.Listbox(footer, width=70, height=len(self.choices), exportselection=False,
                                  background=COLORS["surface"], foreground=COLORS["text"], font="TkDefaultFont",
                                  selectforeground=COLORS["text"], selectbackground=COLORS["selected"])
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
        ttk.Label(footer, text="Up/Down to select · Space or Enter to confirm · Escape to deny", wraplength=600).pack()
        ttk.Button(footer, text="Confirm selection", command=self.choose_option).pack(pady=8)
        self.window.activate()
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
        self.frame.rowconfigure(1, weight=1)
        ttk.Label(self.frame, text="Skills · Up/Down to inspect · Space or Enter to toggle", wraplength=650).grid(
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
        self._shutdown_error: Exception | None = None
        self._skill_panel: SkillPanel | None = None
        self._skill_changing: set[str] = set()
        self._skill_panel_request: object | None = None
        self._skill_metadata: dict[str, dict[str, Any]] = {}
        self._skill_requests: dict[str, object] = {}
        self._permission_queue: dict[str, tuple[str, dict[str, Any], float]] = {}
        self._showing_permission = False
        self._drafts: dict[str, str] = {}
        self._draft_dirty: set[str] = set()
        self._draft_locks: dict[str, Lock] = {}
        self._draft_timer: str | None = None
        self._restoring_draft = False
        self._selected_archived = False
        self._presentation: dict[str, Any] = {"text_size": "default", "reduced_motion": system_reduced_motion(), "sidebar_width": 260, "sidebar_collapsed": False}
        self._presentation_timer: str | None = None
        self._presentation_ready = False
        self._list_request: object | None = None
        self._navigation_request: object | None = None
        self._search_timer: str | None = None
        self._archived_view = False
        self._transcript_widgets: list[tk.Widget] = []
        self._rendered_blocks: list[tuple[tuple, str, list[tk.Widget]]] = []
        self._batching_events = False
        self._render_pending = False
        self._link_sequence = 0
        self._reflow_texts: dict[tk.Text, tuple[str, str, int]] = {}
        self._reflow_sizes: dict[tk.Text, tuple[int, int]] = {}
        self._sidebar_timer: str | None = None
        self._sidebar_target: int | None = None
        self._sidebar_animating = False
        self._layout_timer: str | None = None
        self._session_dropdown: DropdownMenu | None = None
        self._display_scaling = float(root.tk.call("tk", "scaling"))
        self._zoom_percent = 100
        self._zoom_requested = 100
        self._zoom_timer: str | None = None
        self._zoom_themes: dict[int, FluxTheme] = {}
        self._zoom_widgets: WeakKeyDictionary[tk.Misc, dict[str, Any]] = WeakKeyDictionary()
        root._flux_zoom = 100
        self._ui_callbacks: SimpleQueue[Callable[[], None]] = SimpleQueue()

        self.status_text = tk.StringVar(value="Starting local backend…")
        self.detail_text = tk.StringVar(value="Connecting to FLUX")
        self.model_text = tk.StringVar()
        self.workspace_text = tk.StringVar(value=str(self.workspace))
        self.skills_text = tk.StringVar(value="Active skills · None")
        self._build()
        for pattern, direction in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-KP_Add>", 1),
                                   ("<Control-minus>", -1), ("<Control-KP_Subtract>", -1), ("<Control-0>", 0)):
            root.bind_class("FluxZoom", pattern, lambda _event, direction=direction: self._request_zoom(direction))
        root.bind_class("FluxZoom", "<Control-MouseWheel>", self._zoom_wheel)
        root.bind_class("FluxZoom", "<Control-Button-4>", lambda _event: self._request_zoom(1))
        root.bind_class("FluxZoom", "<Control-Button-5>", lambda _event: self._request_zoom(-1))
        self._scale_widget_tree(self.root)
        root.bind_all("<Map>", self._zoom_widget_mapped, add=True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Destroy>", self._destroy_callbacks, add=True)
        self.root.after(25, self._drain_ui_callbacks)
        self._run_task(self._load_application, self._application_loaded)

    def _destroy_callbacks(self, event: tk.Event) -> None:
        """Cancel application timers before the owning Tcl interpreter is destroyed."""
        if event.widget is self.root:
            self._closing = True
            for identifier in self.root.tk.call("after", "info"):
                self.root.after_cancel(identifier)

    def _build(self) -> None:
        """Build the charcoal shell from shared rounded controls and typography."""
        self.root.title("FLUX")
        self.root.geometry("1120x740")
        self.root.minsize(900, 600)
        self.theme = FluxTheme(self.root)
        self._zoom_themes[100] = self.theme
        titlebar = ttk.Frame(self.root, style="Titlebar.TFrame", padding=(12, 6))
        edge = round(8 * self.theme.scale) if sys.platform == "win32" else 0
        if edge:
            self.root.configure(background=COLORS["titlebar"])
        titlebar.pack(fill=tk.X, padx=edge, pady=(edge, 0))
        mark = tk.Canvas(titlebar, width=32, height=32, background=COLORS["titlebar"], highlightthickness=0)
        mark.pack(side=tk.LEFT, padx=(0, 10))
        draw_mark(mark)
        ttk.Label(titlebar, text="FLUX", style="Titlebar.Title.TLabel").pack(side=tk.LEFT)
        ttk.Button(titlebar, text="×", width=3, style="Danger.Titlebar.TButton", command=self.close).pack(side=tk.RIGHT)
        maximize = ttk.Button(titlebar, text="□", width=3, style="Titlebar.TButton", command=lambda: self.window_frame.maximize())
        maximize.pack(side=tk.RIGHT, padx=4)
        ttk.Button(titlebar, text="−", width=3, style="Titlebar.TButton", command=self.root.iconify).pack(side=tk.RIGHT)
        self.connection_label = ttk.Label(titlebar, textvariable=self.status_text, style="Titlebar.Status.TLabel", wraplength=300)
        self.connection_label.pack(side=tk.RIGHT, padx=12)
        self.window_frame = WindowFrame(self.root, titlebar, maximize)
        self.root._flux_frame = self.window_frame
        self.sidebar_rail = ttk.Frame(self.root, style="Titlebar.TFrame", padding=(8, 12))
        self.sidebar_rail.pack(side=tk.LEFT, fill=tk.Y, padx=(edge, 0), pady=(0, edge))
        self.sidebar_toggle = ttk.Button(self.sidebar_rail, text="☰", width=2, style="Titlebar.TButton", command=self.toggle_sidebar)
        self.sidebar_toggle.pack(anchor="n")
        self.sidebar_hint = ContextHint(self.sidebar_toggle, lambda: "Open Side Panel" if self._sidebar_target == 0 else "Close Side Panel")
        self.panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        self.panes.pack(fill=tk.BOTH, expand=True, padx=(0, edge), pady=(0, edge))
        self.navigation = ttk.Frame(self.panes, style="Sidebar.TFrame", width=260)
        self.navigation_content = ttk.Frame(self.navigation, style="Sidebar.TFrame", padding=16)
        self.navigation_content.place(x=0, y=0, width=260, relheight=1)
        self.navigation_content.columnconfigure(0, weight=1)
        self.navigation_content.rowconfigure(3, weight=1)
        self.navigation.bind("<Configure>", self._sidebar_resized)
        self.panes.add(self.navigation, weight=0)
        ttk.Button(self.navigation_content, text="+  New session", style="Sidebar.TButton", command=self.create_conversation).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.search_text = tk.StringVar()
        search_frame = SessionSearch(self.navigation_content, self.search_text)
        search_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        search = search_frame.entry
        search.bind("<KeyRelease>", self._search_changed)
        search.bind("<Escape>", lambda _event: self._clear_search())
        self.group_text = tk.StringVar(value="Sessions")
        ttk.Label(self.navigation_content, textvariable=self.group_text, style="Sidebar.TLabel").grid(row=2, column=0, sticky="w")
        self.conversation_list = SessionList(self.navigation_content, self._conversation_selected, self._toggle_pin, self.session_menu)
        self.conversation_list.grid(row=3, column=0, sticky="nsew", pady=8)
        ContextHint(search, lambda: "Search titles and submitted messages, including archived sessions")
        self.archive_button = ttk.Button(self.navigation_content, text="Archived sessions", style="Sidebar.TButton", command=self.toggle_archived)
        self.archive_button.grid(row=5, column=0, sticky="ew", pady=4)
        ttk.Button(self.navigation_content, text="Settings", style="Sidebar.TButton", command=self.open_settings).grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self.workspace_surface = ttk.Frame(self.panes, padding=(24, 16))
        workspace = self.workspace_surface
        self.root._flux_decision_parent = workspace
        self.panes.add(workspace, weight=1)
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(2, weight=1)
        ttk.Label(workspace, textvariable=self.detail_text, style="Title.TLabel", wraplength=600).grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.notice_text = tk.StringVar()
        self.notice_button = ttk.Button(workspace, textvariable=self.notice_text, command=self._present_next_permission)
        self.transcript = tk.Text(workspace, wrap="word", state="disabled", takefocus=True,
                                  padx=12, pady=16, spacing3=8, highlightthickness=0)
        self.transcript.grid(row=2, column=0, sticky="nsew")
        self.chat_scrollbar = ttk.Scrollbar(workspace, orient=tk.VERTICAL, style="Chat.Vertical.TScrollbar", command=self.transcript.yview)
        self.chat_scrollbar.grid(row=2, column=1, sticky="ns", padx=(8, 0))
        self.transcript.configure(yscrollcommand=self._scrolled)
        self.transcript.bind("<Configure>", self._queue_layout)
        self.transcript.tag_configure("user", justify="right", foreground=COLORS["text"], rmargin=8, spacing3=16)
        self.transcript.tag_configure("assistant", foreground=COLORS["text"], spacing3=6)
        self.transcript.tag_configure("status", foreground=COLORS["muted"], lmargin1=12, lmargin2=12)
        self.transcript.tag_configure("failure", foreground=COLORS["failure"])
        self.transcript.tag_configure("success", foreground=COLORS["success"])
        self.transcript.tag_configure("heading", foreground=COLORS["accent"], font=(FONT_FAMILY, 14, "bold"), spacing1=12)
        self.transcript.tag_configure("bold", font=(FONT_FAMILY, 11, "bold"))
        self.transcript.tag_configure("italic", font=(FONT_FAMILY, 11, "italic"))
        self.transcript.tag_configure("code", font="TkFixedFont", background=COLORS["surface"])
        self.jump_button = ttk.Button(workspace, text="Jump to latest ↓", command=self.jump_to_latest)
        composer = ttk.Frame(workspace, style="Surface.TFrame", padding=12)
        composer.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        composer.columnconfigure(0, weight=1)
        self.message_input = tk.Text(composer, height=2, wrap="word", background=COLORS["surface"],
                                    highlightthickness=0, padx=8, pady=8, undo=True)
        self.message_input.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.message_input.bind("<Return>", self._send_key)
        self.message_input.bind("<Shift-Return>", self._newline_key)
        self.message_input.bind("<Control-Return>", self._send_key)
        self.message_input.bind("<<Modified>>", self._draft_modified)
        self.composer_placeholder = ttk.Label(composer, text="Message FLUX…", style="Surface.TLabel", takefocus=False)
        self.composer_placeholder.bind("<Button-1>", lambda _event: self.message_input.focus_set())
        self.message_input.bind("<FocusIn>", self._update_composer_placeholder)
        self.message_input.bind("<FocusOut>", self._update_composer_placeholder)
        controls = ttk.Frame(composer, style="Inset.TFrame")
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.model_selector = DropdownButton(controls, self.model_text, lambda: self.models,
                                             lambda _value: self._model_selected(None), width=18, style="Inset.TButton")
        self.model_selector.grid(row=0, column=0, padx=(0, 8))
        self.workspace_button = ttk.Button(controls, text="Workspace…", style="Inset.TButton", command=self.choose_workspace)
        self.workspace_button.grid(row=0, column=1, padx=(0, 8))
        self.skills_button = ttk.Button(controls, text="Skills…", style="Inset.TButton", command=self.open_skills)
        self.skills_button.grid(row=0, column=2)
        ContextHint(self.workspace_button, lambda: "Workspace · " + self.workspace_text.get())
        ContextHint(self.skills_button, self.skills_text.get)
        ContextHint(self.model_selector, lambda: "Model · " + self.model_text.get())
        self._skill_container = ttk.Frame(workspace)
        self._skill_container.columnconfigure(0, weight=1)
        self._skill_container.rowconfigure(0, weight=1)
        self.send_button = ttk.Button(composer, text="↑", width=2, style="Send.TButton", command=self.send_turn)
        self.send_button.grid(row=1, column=1, sticky="e", padx=(12, 0))
        ContextHint(self.send_button, lambda: "Stop active turn" if self.views.get(self.active_conversation_id) and self.views[self.active_conversation_id].active_turn_id else "Send message · Enter")
        # Retain the cancellation seam while the visible action becomes Stop during work.
        self.cancel_button = ttk.Button(composer, text="Stop", command=self.cancel_turn)
        self.retry_turn_button = ttk.Button(workspace, text="Retry failed turn", command=self.retry_turn)
        self.retry_button = ttk.Button(workspace, text="Reconnect", command=self.refresh)
        self.panes.bind("<ButtonRelease-1>", lambda _event: self._remember_sidebar())
        self.root.bind("<Configure>", self._window_changed, add=True)
        self.root.bind("<Control-n>", lambda _event: self.create_conversation())
        self.root.bind("<Control-b>", lambda _event: self.toggle_sidebar())
        self._render_conversation()
        self.root.after_idle(lambda: self._apply_sidebar(animate=False))
        self._run_task(self.client.presentation, self._presentation_loaded)

    def _presentation_loaded(self, values: dict[str, Any]) -> None:
        """Restore validated preferences and clamp the window to a usable monitor."""
        if not isinstance(values, dict):
            return
        self._presentation_ready = True
        self._presentation.update(values)
        if values.get("zoom_percent", 100) != self._zoom_percent:
            self._zoom_requested = values["zoom_percent"]
            self._apply_zoom()
        self._apply_typography()
        window = values.get("window", {})
        if window:
            bounds = display_bounds(window["x"], window["y"], (self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
            self.window_frame.restore_geometry(window, bounds)
            if window.get("maximized"):
                self.root.state("zoomed")
        self._apply_sidebar(animate=False)

    def _apply_typography(self) -> None:
        """Apply consistent text-size preferences to shared fonts and Markdown tags."""
        size = self._presentation["text_size"]
        self.theme.set_text_size(size)
        points = round(TEXT_SIZES[size] * getattr(self, "_zoom_percent", 100) / 100)
        for tag, weight in (("bold", "bold"), ("italic", "italic")):
            self.transcript.tag_configure(tag, font=(FONT_FAMILY, points, weight))
        self.transcript.tag_configure("heading", font=(FONT_FAMILY, round((TEXT_SIZES[size] + 3) * getattr(self, "_zoom_percent", 100) / 100), "bold"))

    def _zoom_wheel(self, event: tk.Event) -> str:
        """Consume Ctrl-wheel for interface zoom before text or session scrolling."""
        if event.delta:
            self._request_zoom(1 if event.delta > 0 else -1)
        return "break"

    def _request_zoom(self, direction: int) -> str:
        """Coalesce bounded zoom steps, with zero restoring the default percentage."""
        index = ZOOM_LEVELS.index(self._zoom_requested)
        self._zoom_requested = 100 if direction == 0 else ZOOM_LEVELS[max(0, min(len(ZOOM_LEVELS) - 1, index + direction))]
        if self._zoom_timer is None and self._zoom_requested != self._zoom_percent:
            self._zoom_timer = self.root.after(40, self._apply_zoom)
        return "break"

    def _apply_zoom(self) -> None:
        """Scale the complete interface while retaining sessions, drafts, and native geometry."""
        if self._zoom_timer is not None:
            self.root.after_cancel(self._zoom_timer)
        self._zoom_timer = None
        percent = self._zoom_requested
        if self._closing or percent == self._zoom_percent:
            return
        for widget in list(self._zoom_widgets):
            if isinstance(widget, DropdownButton) and widget.menu and not widget.menu.closed:
                widget.menu.close()
        if self._session_dropdown and not self._session_dropdown.closed:
            self._session_dropdown.close()
        self._zoom_percent = percent
        self.root._flux_zoom = percent
        self.root.tk.call("tk", "scaling", self._display_scaling)
        if percent not in self._zoom_themes:
            self._zoom_themes[percent] = FluxTheme(self.root, f"flux-{percent}")
        self.theme = self._zoom_themes[percent]
        self.theme.style.theme_use("flux" if percent == 100 else f"flux-{percent}")
        if sys.platform == "win32":
            self.root.configure(background=COLORS["titlebar"])
        self._presentation["zoom_percent"] = percent
        self._apply_typography()
        self._scale_widget_tree(self.root)
        self._sidebar_auto_collapsed = False
        self._apply_sidebar(animate=False)
        self._render_conversation()
        self._save_presentation_later()

    def _zoom_widget_mapped(self, event: tk.Event) -> None:
        """Apply shortcut routing and spacing to newly opened controls and decision views."""
        if isinstance(event.widget, tk.Misc) and event.widget not in self._zoom_widgets:
            self._scale_widget_tree(event.widget)

    def _scale_widget_tree(self, widget: tk.Misc) -> None:
        """Scale pixel spacing and icons from original values without accumulating rounding."""
        if not hasattr(self, "_zoom_widgets"):
            return
        tags = widget.bindtags()
        if "FluxZoom" not in tags:
            widget.bindtags(("FluxZoom", *tags))
        saved = self._zoom_widgets.get(widget)
        if saved is None:
            keys = widget.keys()
            options = {key: widget.cget(key) for key in ("padding", "padx", "pady", "wraplength") if key in keys}
            if isinstance(widget, (tk.Canvas, ttk.Frame)):
                options.update({key: widget.cget(key) for key in ("width", "height") if key in keys})
            manager = widget.winfo_manager()
            geometry = getattr(widget, manager + "_info")() if manager in {"pack", "grid", "place"} else {}
            saved = {"options": options, "manager": manager, "geometry": geometry, "percent": 100}
            self._zoom_widgets[widget] = saved
        percent = self._zoom_percent
        if saved["percent"] != percent:
            def pixels(value: Any) -> tuple[int, ...]:
                """Scale Tcl pixel tuples using the original unzoomed dimensions."""
                return tuple(round(float(str(item)) * percent / 100) for item in (value if isinstance(value, (tuple, list)) else widget.tk.splitlist(str(value))))
            changes = {key: pixels(value) for key, value in saved["options"].items() if str(value)}
            if changes:
                widget.configure(**changes)
            manager = saved["manager"]
            if manager == widget.winfo_manager():
                geometry = saved["geometry"]
                names = ("x", "y", "width", "height") if manager == "place" else ("padx", "pady", "ipadx", "ipady")
                spacing = {key: pixels(geometry[key]) for key in names if key in geometry and str(geometry[key])}
                if spacing:
                    getattr(widget, manager + "_configure")(**spacing)
            if isinstance(widget, tk.Canvas):
                ratio = percent / saved["percent"]
                widget.scale("all", 0, 0, ratio, ratio)
            if isinstance(widget, (DropdownButton, SessionSearch)):
                widget.refresh_icon()
            saved["percent"] = percent
        for child in widget.winfo_children():
            self._scale_widget_tree(child)

    def _apply_sidebar(self, *, animate: bool = True) -> None:
        """Adapt constrained layouts without overwriting the user's sidebar preference."""
        threshold = 1160 if getattr(self, "_sidebar_auto_collapsed", False) else 1040
        self._sidebar_auto_collapsed = self.root.winfo_width() < threshold * self.theme.scale
        collapse = self._presentation["sidebar_collapsed"] or self._sidebar_auto_collapsed
        self._set_sidebar_open(not collapse, animate=animate)

    def _set_sidebar_open(self, opened: bool, *, animate: bool = True) -> None:
        """Slide a clipped, fixed-width sidebar and honor reduced-motion preferences."""
        width = round(self._presentation["sidebar_width"] * self.theme.scale)
        target = width if opened else 0
        present = str(self.navigation) in self.panes.panes()
        if target == self._sidebar_target and (present == opened or self._sidebar_animating):
            return
        if self._sidebar_timer:
            self.root.after_cancel(self._sidebar_timer)
            self._sidebar_timer = None
        self._sidebar_target = target
        if hasattr(self, "sidebar_hint"):
            self.sidebar_hint.refresh()
        self._sidebar_animating = False
        if not present and not opened:
            return
        if not present:
            self.panes.insert(0, self.navigation, weight=0)
            self.panes.sashpos(0, 0)
        current = self.panes.sashpos(0)
        self.navigation_content.place_configure(width=max(width, current))
        if not animate or self._presentation["reduced_motion"] or not self.root.winfo_ismapped():
            if opened:
                self.panes.sashpos(0, target)
            else:
                self.panes.forget(self.navigation)
            self._queue_layout()
            return
        self._sidebar_animating = True
        started = time.monotonic()
        def step() -> None:
            """Advance one bounded animation frame, finishing at the requested pane state."""
            self._sidebar_timer = None
            if self._closing:
                return
            progress = min(1.0, (time.monotonic() - started) * 1000 / MOTION["sidebar_ms"])
            eased = 1 - (1 - progress) ** 3
            self.panes.sashpos(0, round(current + (target - current) * eased))
            if progress < 1:
                self._sidebar_timer = self.root.after(MOTION["frame_ms"], step)
            else:
                self._sidebar_animating = False
                if not opened:
                    self.panes.forget(self.navigation)
                else:
                    self.navigation_content.place_configure(width=self.navigation.winfo_width())
                self._queue_layout()
        step()

    def _sidebar_resized(self, event: tk.Event) -> None:
        """Resize sidebar content on sash changes while clipping it during transitions."""
        if not self._sidebar_animating:
            self.navigation_content.place_configure(width=event.width)

    def toggle_sidebar(self) -> None:
        """Reverse an in-progress transition or toggle the remembered sidebar state."""
        opened = self._sidebar_target != 0 if self._sidebar_target is not None else str(self.navigation) in self.panes.panes()
        self._presentation["sidebar_collapsed"] = opened
        if not opened:
            self._sidebar_auto_collapsed = False
        self._set_sidebar_open(not opened)
        self._save_presentation_later()

    def _remember_sidebar(self) -> None:
        """Persist a bounded sidebar width after the user finishes dragging its sash."""
        if not self._sidebar_animating and str(self.navigation) in self.panes.panes():
            self._presentation["sidebar_width"] = max(200, min(420, round(self.panes.sashpos(0) / self.theme.scale)))
            self.panes.sashpos(0, round(self._presentation["sidebar_width"] * self.theme.scale))
            self._sidebar_target = round(self._presentation["sidebar_width"] * self.theme.scale)
            self._save_presentation_later()

    def _window_changed(self, event: tk.Event) -> None:
        """Debounce geometry persistence and adapt the sidebar only on root resizes."""
        if event.widget is not self.root or self._closing:
            return
        self._apply_sidebar(animate=False)
        if self.root.winfo_width() < 1050 or float(self.root.tk.call("tk", "scaling")) > 2:
            self.connection_label.pack_forget()
        elif not self.connection_label.winfo_manager():
            self.connection_label.pack(side=tk.RIGHT, padx=12)
        if self.root.state() == "normal":
            self._presentation["window"] = {"width": max(900, self.root.winfo_width()),
                                            "height": max(600, self.root.winfo_height()),
                                            "x": self.root.winfo_x(), "y": self.root.winfo_y(), "maximized": False}
        elif "window" in self._presentation:
            self._presentation["window"]["maximized"] = self.root.state() == "zoomed"
        self._save_presentation_later()
        self._queue_layout()

    def _queue_layout(self, _event: tk.Event | None = None) -> None:
        """Coalesce resize work at frame cadence without waiting for resizing to stop."""
        if self._layout_timer is None and not self._closing:
            self._layout_timer = self.root.after(MOTION["frame_ms"], self._layout_changed)

    def _layout_changed(self) -> None:
        """Reflow existing selectable text in place instead of rebuilding conversation widgets."""
        self._layout_timer = None
        if self._closing:
            return
        bottom = self.transcript.yview()[1] >= 0.97
        self._reflow_embedded()
        if bottom:
            self.transcript.see(tk.END)

    def _track_reflow(self, widget: tk.Text, kind: str, display: str, max_rows: int) -> None:
        """Track only live embedded text and release layout metadata on destruction."""
        self._reflow_texts[widget] = (kind, display, max_rows)
        self._reflow_sizes[widget] = (int(widget.cget("width")), int(widget.cget("height")))
        widget.bind("<Destroy>", lambda event: self._forget_reflow(widget, event), add=True)

    def _forget_reflow(self, widget: tk.Text, event: tk.Event) -> None:
        """Release dimension caches with their owning embedded widget."""
        if event.widget is widget:
            self._reflow_texts.pop(widget, None)
            self._reflow_sizes.pop(widget, None)

    def _reflow_embedded(self) -> None:
        """Reflow visible text in place, deferring offscreen widgets until they enter view."""
        widths = {False: self._content_columns(), True: self._content_columns(code=True)}
        for widget, (kind, display, max_rows) in list(self._reflow_texts.items()):
            width = widths[kind == "code"]
            cached = self._reflow_sizes.get(widget)
            if cached and cached[0] == width:
                continue
            if not widget.winfo_viewable():
                continue
            if cached is None:
                cached = (int(widget.cget("width")), int(widget.cget("height")))
            if kind == "code":
                height = min(max_rows, max(2, display.count("\n") + 1))
            else:
                height = min(max_rows, max(1, sum(max(1, (len(line) + width - 1) // width) for line in display.splitlines())))
            if cached != (width, height):
                widget.configure(width=width, height=height)
            self._reflow_sizes[widget] = (width, height)

    def _save_presentation_later(self) -> None:
        """Persist one coalesced presentation snapshot after resize and appearance changes."""
        if not self._presentation_ready:
            return
        if self._presentation_timer:
            self.root.after_cancel(self._presentation_timer)
        self._presentation_timer = self.root.after(600, self._save_presentation)

    def _save_presentation(self) -> None:
        """Capture independent preferences before dispatching a backend write."""
        self._presentation_timer = None
        values = json.loads(json.dumps(self._presentation))
        self._run_task(lambda: self.client.save_presentation(values))

    def _newline_key(self, _event: object) -> str:
        """Insert a newline without submitting or queuing a message."""
        self.message_input.insert(tk.INSERT, "\n")
        return "break"

    def _draft_modified(self, _event: object = None) -> None:
        """Track separate drafts and grow the composer within its height limit."""
        if not self.message_input.edit_modified():
            return
        self.message_input.edit_modified(False)
        self._update_composer_placeholder()
        if self._restoring_draft:
            return
        text = self.message_input.get("1.0", "end-1c")
        self.message_input.configure(height=min(8, max(2, text.count("\n") + 1)))
        owner = self.active_conversation_id
        if owner:
            self._drafts[owner] = text
            self._draft_dirty.add(owner)
            if self._draft_timer:
                self.root.after_cancel(self._draft_timer)
            self._draft_timer = self.root.after(500, self._flush_drafts)

    def _write_draft(self, owner: str, snapshot: str) -> None:
        """Serialize draft writes and skip snapshots replaced by navigation or submission."""
        lock = self._draft_locks.setdefault(owner, Lock())
        with lock:
            if self._drafts.get(owner) == snapshot:
                self.client.session_state(owner, "draft", snapshot)

    def _flush_drafts(self) -> None:
        """Save dirty session drafts without adding them to submitted history."""
        self._draft_timer = None
        for owner in list(self._draft_dirty):
            snapshot = self._drafts.get(owner, "")
            self._draft_dirty.discard(owner)
            if len(snapshot) > 16000:
                self.status_text.set("Draft exceeds the saved-draft limit of 16,000 characters.")
                continue
            self._run_task(lambda owner=owner, snapshot=snapshot: self._write_draft(owner, snapshot),
                           failed=lambda error, owner=owner: self._draft_failed(owner, error))

    def _draft_failed(self, owner: str, error: Exception) -> None:
        """Retain unsaved drafts and surface persistence failures for explicit recovery."""
        self._draft_dirty.add(owner)
        self.status_text.set("Draft not saved · " + str(error))

    def _toggle_pin(self, owner: str) -> None:
        """Toggle only the clicked card's pin without changing the active chat."""
        metadata = next((item for item in self.conversations if item["conversation_id"] == owner), None)
        if metadata:
            self._change_session(owner, "pinned", not metadata.get("pinned", False))

    def _search_changed(self, _event: object = None) -> None:
        """Debounce bounded title and message-content searches across archived sessions."""
        if self._search_timer:
            self.root.after_cancel(self._search_timer)
        self._search_timer = self.root.after(250, self._reload_sessions)

    def _clear_search(self) -> str:
        """Clear the search and return to the current session group."""
        self.search_text.set("")
        self._reload_sessions()
        return "break"

    def _reload_sessions(self) -> None:
        """Refresh navigation without changing the selected chat or restoring stale searches."""
        self._search_timer = None
        query, archived = self.search_text.get().strip(), self._archived_view
        token = object()
        self._list_request = token
        if len(query) > 200:
            self.status_text.set("Search is limited to 200 characters.")
            return
        def loaded(items: list[dict[str, Any]]) -> None:
            """Apply only the latest navigation query, keeping the chat selected."""
            if self._list_request is token:
                self._replace_conversations(items, select=False)
        self._run_task(lambda: self.client.list_conversations(archived=archived, search=query), loaded)

    def toggle_archived(self) -> None:
        """Switch the sidebar between active and archived sessions."""
        self._archived_view = not self._archived_view
        self.search_text.set("")
        self.archive_button.configure(text="Back to sessions" if self._archived_view else "Archived sessions")
        self._reload_sessions()

    def session_menu(self, owner: str | None = None) -> None:
        """Show keyboard-accessible actions for the selected stable session identity."""
        owner = owner or self.active_conversation_id
        if not owner:
            return
        if any(isinstance(child, (tk.Toplevel, InlineDecision)) for child in self.root.winfo_children()):
            self.status_text.set("Close the current decision view before opening session actions.")
            return
        metadata = next((item for item in self.conversations if item["conversation_id"] == owner), self._skill_metadata.get(owner, {}))
        choices = [("Rename session", "rename"),
                   ("Unarchive session" if metadata.get("archived") else "Archive session", "archive"),
                   ("Permanently delete session…", "delete")]
        def chosen(action: str) -> None:
            """Execute the captured action without redirecting it after navigation."""
            if action == "rename":
                self._rename_session(owner, metadata.get("title", ""))
            else:
                view = self.views.get(owner)
                if view and (view.sending or view.active_turn_id or view.transport_active):
                    self.status_text.set("Stop active work before archiving or deleting this session.")
                    return
                if action == "archive":
                    self._change_session(owner, "archived", not metadata.get("archived", False))
                elif action == "delete":
                    DecisionDialog(self.root, "Delete session permanently?", "Submitted history and the saved draft will be deleted. Workspace files and interaction logs are retained.",
                                   [("Keep session", "keep"), ("Delete permanently", "delete")],
                                   lambda answer: self._delete_session(owner) if answer == "delete" else None)
        row = self.conversation_list.rows.get(owner)
        if self._session_dropdown and not self._session_dropdown.closed:
            same_anchor = bool(row and self._session_dropdown.anchor is row[4])
            self._session_dropdown.close()
            if same_anchor:
                return
        if row:
            self._session_dropdown = DropdownMenu(row[4], choices, chosen)

    def _rename_session(self, owner: str, title: str) -> None:
        """Edit a deterministic session title without changing retained conversation context."""
        window = InlineDecision(self.root)
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Rename session", style="Title.TLabel", wraplength=560).pack(anchor="w", pady=(0, 12))
        value = tk.StringVar(value=title)
        entry = ttk.Entry(frame, textvariable=value, width=50)
        entry.pack(fill=tk.X)
        def save(_event: object = None) -> str:
            """Submit the captured title after ordinary nonempty validation."""
            text = value.get().strip()
            if text and len(text) <= 200:
                window.destroy()
                self._change_session(owner, "title", text)
            return "break"
        ttk.Button(frame, text="Save title", command=save).pack(anchor="e", pady=12)
        window.bind("<Return>", save)
        window.bind("<Escape>", lambda _event: window.destroy())
        window.activate()
        entry.focus_set()

    def _change_session(self, owner: str, field: str, value: Any) -> None:
        """Apply a backend-owned session change and refresh its visible metadata."""
        def changed(metadata: dict[str, Any]) -> None:
            """Keep the selected chat while refreshing groups and action labels."""
            self._skill_metadata[owner] = metadata
            if owner == self.active_conversation_id:
                self.detail_text.set(metadata["title"])
                self._selected_archived = metadata.get("archived", False)
                self._render_conversation()
            self._reload_sessions()
        self._run_task(lambda: self.client.session_state(owner, field, value), changed)

    def _delete_session(self, owner: str) -> None:
        """Delete one idle session and discard only its local presentation state."""
        def deleted(_result: dict[str, Any]) -> None:
            """Remove the deleted session without altering other conversations or drafts."""
            self.views.pop(owner, None)
            self._drafts.pop(owner, None)
            self._draft_dirty.discard(owner)
            self._skill_metadata.pop(owner, None)
            if owner == self.active_conversation_id:
                self.active_conversation_id = None
                self.message_input.delete("1.0", tk.END)
                self._render_conversation()
            self._reload_sessions()
        self._flush_drafts()
        self._run_task(lambda: self.client.delete_conversation(owner), deleted)

    def _scrolled(self, first: str, last: str) -> None:
        """Update the rounded session scrollbar and expose a jump action away from latest."""
        self.chat_scrollbar.set(first, last)
        self.chat_scrollbar.state(["disabled"] if float(last) - float(first) >= 0.999 else ["!disabled"])
        self._queue_layout()
        if not hasattr(self, "jump_button"):
            return
        view = self.views.get(self.active_conversation_id)
        if view and view.turns and not self._skill_panel and float(last) < 0.97:
            self.jump_button.grid(row=3, column=0, sticky="e", pady=4)
        else:
            self.jump_button.grid_remove()

    def jump_to_latest(self) -> None:
        """Resume following streamed output at the bottom of the selected conversation."""
        self.transcript.see(tk.END)
        self.jump_button.grid_remove()

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
        self.window_frame.sync_hover()
        self.window_frame.sync_window_state()
        self._batching_events = True
        try:
            for _ in range(100):
                self._ui_callbacks.get_nowait()()
        except Empty:
            pass
        finally:
            self._batching_events = False
            if self._render_pending:
                self._render_pending = False
                self._render_conversation()
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

    def _replace_conversations(self, conversations: list[dict[str, Any]], *, select: bool = True) -> None:
        """Replace navigation items while keeping backend identifiers out of labels."""
        if self._session_dropdown and not self._session_dropdown.closed:
            self._session_dropdown.close(restore_focus=False)
        self.conversations = conversations
        self.group_text.set("Search results" if self.search_text.get().strip() else "Archived sessions" if self._archived_view else "Pinned ◆ / Recent")
        pending = {owner for owner, _, _ in self._permissions.values()} | {owner for owner, _, _ in self._permission_queue.values()}
        self.conversation_list.replace(conversations, self.active_conversation_id, pending)
        selected = next((item for item in conversations if item["conversation_id"] == self.active_conversation_id), None)
        view = self.views.get(self.active_conversation_id)
        if selected and not (view and view.error) and self.status_text.get() != "Needs attention":
            self.detail_text.set(selected.get("title", "New session"))
        if conversations and select and (self.active_conversation_id is None or selected):
            index = next((index for index, item in enumerate(conversations)
                          if item["conversation_id"] == self.active_conversation_id), 0)
            self._select_conversation(conversations[index])
            owner = conversations[index]["conversation_id"]
            def hydrated(metadata: dict[str, Any]) -> None:
                """Hydrate a saved draft without replacing text typed during startup."""
                self._skill_metadata[owner] = metadata
                if owner not in self._draft_dirty and not self._drafts.get(owner) and (owner != self.active_conversation_id or not self.message_input.get("1.0", "end-1c")):
                    self._drafts[owner] = metadata.get("draft", "")
                    if self.active_conversation_id == owner:
                        self._restoring_draft = True
                        self.message_input.delete("1.0", tk.END)
                        self.message_input.insert("1.0", self._drafts[owner])
                        self.message_input.edit_modified(False)
                        self._restoring_draft = False
                if self.active_conversation_id == owner:
                    self._selected_archived = metadata.get("archived", False)
            self._run_task(lambda: self.client.get_conversation(owner), hydrated)

    def _conversation_selected(self, conversation_id: str) -> None:
        """Load the clicked card's metadata using its captured backend identity."""
        token = object()
        self._navigation_request = token
        def selected(metadata: dict[str, Any]) -> None:
            """Ignore an older navigation response after a newer session was selected."""
            if self._navigation_request is token:
                self._select_conversation(metadata)
        self._run_task(lambda: self.client.get_conversation(conversation_id), selected)

    def _select_conversation(self, conversation: dict[str, Any]) -> None:
        """Display one conversation's backend-owned selections."""
        self._navigation_request = None
        changed = self.active_conversation_id != conversation["conversation_id"]
        if changed:
            if self.active_conversation_id:
                self._drafts[self.active_conversation_id] = self.message_input.get("1.0", "end-1c")
                self._draft_dirty.add(self.active_conversation_id)
                self._flush_drafts()
                previous = self.views.get(self.active_conversation_id)
                if previous:
                    previous.scroll_index = self.transcript.index("@0,0")
            self.close_skills()
        self.active_conversation_id = conversation["conversation_id"]
        self.conversation_list.set_active(self.active_conversation_id)
        self._selected_archived = conversation.get("archived", False)
        if changed:
            self._restoring_draft = True
            self.message_input.delete("1.0", tk.END)
            self.message_input.insert("1.0", self._drafts.setdefault(self.active_conversation_id, conversation.get("draft", "")))
            self.message_input.edit_modified(False)
            self._restoring_draft = False
        self.detail_text.set(conversation.get("title", "New conversation"))
        self.model_text.set(conversation.get("model", ""))
        self.workspace_text.set(conversation.get("workspace", str(self.workspace)))
        self._skill_metadata[self.active_conversation_id] = conversation
        self._render_skills()
        self.views.setdefault(self.active_conversation_id, ConversationView())
        self._render_conversation()
        if changed:
            self.transcript.yview(self.views[self.active_conversation_id].scroll_index)
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
                    self._permission_queue.pop(request_id, None)
                    if pending:
                        pending[2].dismiss()
                        self._showing_permission = False
                    self._decided_permissions.add(request_id)
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
        """Retain unchanged turn widgets and repaint changed tails once per event batch."""
        if self._batching_events:
            self._render_pending = True
            return
        view = self.views.get(self.active_conversation_id)
        bottom = self.transcript.yview()[1] >= 0.97
        top = self.transcript.index("@0,0")
        layout = (self.active_conversation_id, self._presentation["text_size"])
        grouped: dict[str, list[dict[str, Any]]] = {}
        if view:
            for event in view.events.values():
                grouped.setdefault(event["turn_id"], []).append(event)
        blocks: list[tuple[tuple, str, str | None]] = []
        if not view or not view.turns and not view.pending_message:
            blocks.append(((layout, "welcome"), "welcome", None))
        if view:
            for turn_id, turn in view.turns.items():
                events = sorted(grouped.get(turn_id, []), key=lambda event: event["sequence"])
                grouped[turn_id] = events
                signature = (layout, turn_id, turn.get("user_prompt", ""), turn.get("status"),
                             tuple((event["sequence"], id(event)) for event in events),
                             tuple(str(stored.get("content", "")) for stored in turn.get("events", []) if stored.get("type") == "model_response") if not events else (),
                             view.active_turn_id == turn_id, turn_id in view.expanded_working,
                             tuple(sorted(item for item in view.expanded_operations if item[0] == turn_id)),
                             tuple(sorted(item for item in view.expanded_content if item.startswith(("user:" + turn_id, turn_id + ":")))))
                blocks.append((signature, "turn", turn_id))
            if view.pending_message:
                blocks.append(((layout, "pending", view.pending_message, "pending" in view.expanded_content), "pending", None))
            if view.error:
                blocks.append(((layout, "error", view.error), "error", None))
        common = 0
        while common < min(len(blocks), len(self._rendered_blocks)) and blocks[common][0] == self._rendered_blocks[common][0]:
            common += 1
        self.transcript.configure(state="normal")
        if common < len(self._rendered_blocks):
            boundary = self._rendered_blocks[common][1]
            for tag in self.transcript.tag_names():
                if tag.startswith("link"):
                    ranges = self.transcript.tag_ranges(tag)
                    if not ranges or self.transcript.compare(ranges[0], ">=", boundary):
                        self.transcript.tag_delete(tag)
            self.transcript.delete(boundary, "end-1c")
        for _, mark, widgets in self._rendered_blocks[common:]:
            for widget in widgets:
                widget.destroy()
            self.transcript.mark_unset(mark)
        self._rendered_blocks = self._rendered_blocks[:common]
        self._transcript_widgets = [widget for _, _, widgets in self._rendered_blocks for widget in widgets]
        if common == 0:
            self.transcript.delete("1.0", tk.END)
            for tag in self.transcript.tag_names():
                if tag.startswith("link"):
                    self.transcript.tag_delete(tag)
        for signature, kind, turn_id in blocks[common:]:
            mark = "block:" + str(len(self._rendered_blocks))
            self.transcript.mark_set(mark, "end-1c")
            self.transcript.mark_gravity(mark, tk.LEFT)
            first_widget = len(self._transcript_widgets)
            if kind == "welcome":
                logo = tk.Canvas(self.transcript, width=96, height=96, background=COLORS["canvas"], highlightthickness=0)
                draw_mark(logo, 96)
                self._embed(logo, "welcome")
                self.transcript.tag_configure("welcome", justify="center", spacing1=16, spacing3=12)
                self.transcript.insert(tk.END, "What are we doing today?\n", "welcome")
            elif kind == "turn":
                self._render_turn(view, turn_id, view.turns[turn_id], grouped[turn_id])
            elif kind == "pending":
                self._user_bubble(view.pending_message, "pending", view)
            else:
                self.transcript.insert(tk.END, "\n" + view.error + "\n", "failure")
            self._rendered_blocks.append((signature, mark, self._transcript_widgets[first_widget:]))
        self.transcript.configure(state="disabled")
        self._reflow_embedded()
        if bottom:
            self.transcript.see(tk.END)
        else:
            self.transcript.yview(top)
        busy = self.active_conversation_id in self._skill_changing or bool(view and (not view.history_loaded or view.sending or view.transport_active or view.active_turn_id))
        active = bool(view and view.active_turn_id)
        self.send_button.configure(text="■" if active else "↑", command=self.cancel_turn if active else self.send_turn,
                                   state="normal" if active or view and not busy and not view.error and not self._selected_archived else "disabled")
        self.cancel_button.configure(state="normal" if active else "disabled")
        if view and self._retry_message(view):
            self.retry_turn_button.grid(row=4, column=0, sticky="w", pady=4)
        else:
            self.retry_turn_button.grid_remove()
        if view and view.error:
            self.retry_button.grid(row=4, column=1, sticky="e", pady=4)
        else:
            self.retry_button.grid_remove()
        self.model_selector.configure(state="disabled" if busy else "normal")
        self.workspace_button.configure(state="disabled" if busy else "normal")
        self._update_composer_placeholder()

    def _update_composer_placeholder(self, _event: object = None) -> None:
        """Show a visual prompt hint without inserting it into drafts or submitted messages."""
        if not self.message_input.get("1.0", "end-1c") and self.message_input.focus_get() is not self.message_input:
            zoom = getattr(self, "_zoom_percent", 100) / 100
            self.composer_placeholder.place(in_=self.message_input, x=round(10 * zoom), y=round(8 * zoom))
        else:
            self.composer_placeholder.place_forget()


    def _render_turn(self, view: ConversationView, turn_id: str, turn: dict[str, Any], events: list[dict[str, Any]]) -> None:
        """Render one changed turn while keeping preceding selectable content alive."""
        self._user_bubble(turn.get("user_prompt", ""), "user:" + turn_id, view)
        subset = ConversationView()
        subset.turns = {turn_id: turn}
        subset.events = {(turn_id, event["sequence"]): event for event in events}
        activity = [(tag, text) for tag, text in subset.render()
                    if (tag == "status" and "Working" in text) or (tag == "code" and text.startswith("Result ·"))]
        if activity:
            self._working_section(view, turn_id, activity)
        # Preserve interleaved assistant, permission receipt, and terminal event order.
        assistant = ""
        part = 0
        for event in events:
            if event["type"] == "assistant_text":
                assistant += str(event.get("text", ""))
                continue
            if assistant:
                self._markdown(assistant, f"{turn_id}:{part}", view)
                assistant = ""
                part += 1
            kind = event["type"]
            if kind == "permission_receipt":
                label = PERMISSION_CHOICES.get(event.get("decision"), "Denied")
                scope = str(event.get("scope", "Requested scope"))[:300]
                self.transcript.insert(tk.END, f"\n    Permission · {label} · {scope}\n", "status")
            elif kind == "permission_grant" and event.get("action") == "reused":
                self.transcript.insert(tk.END, f"\n    Permission · Allowed · {event.get('lifetime')} grant reused\n", "status")
            elif kind in {"failure", "interruption"}:
                self.transcript.insert(tk.END, f"\n{kind.title()} · {str(event.get('message') or 'Turn stopped.')[:DISPLAY_TEXT_LIMIT]}\n", "failure")
            elif kind == "completion" and event.get("outcome") != "success":
                self.transcript.insert(tk.END, f"\nTurn {event.get('outcome', 'unknown')}. Send a follow-up to continue with retained results.\n", "status")
        if assistant:
            self._markdown(assistant, f"{turn_id}:{part}", view)
        if not events:
            for stored in turn.get("events", []):
                if stored.get("type") == "model_response" and stored.get("content"):
                    self._markdown(str(stored["content"]), turn_id, view)
            if turn.get("status") != "running":
                self.transcript.insert(tk.END, f"Turn {turn.get('status')}. Send a follow-up to continue.\n", "status")
        self.transcript.insert(tk.END, "\n")

    def _embed(self, widget: tk.Widget, tag: str = "assistant", *, newline: bool = True) -> None:
        """Retain and tag an embedded rounded component for deterministic cleanup."""
        self._transcript_widgets.append(widget)
        self._scale_widget_tree(widget)
        self._bind_chat_wheel(widget)
        start = self.transcript.index("end-1c")
        self.transcript.window_create(tk.END, window=widget, padx=round(4 * getattr(self, "_zoom_percent", 100) / 100), pady=round(6 * getattr(self, "_zoom_percent", 100) / 100))
        self.transcript.tag_add(tag, start, "end-1c")
        self.transcript.insert(tk.END, "\n" if newline else " ", tag)

    def _bind_chat_wheel(self, widget: tk.Widget) -> None:
        """Route scrolling over embedded controls through the same conversation surface."""
        widget.bind("<MouseWheel>", self._chat_wheel)
        for child in widget.winfo_children():
            self._bind_chat_wheel(child)

    def _chat_wheel(self, event: tk.Event) -> str | None:
        """Scroll expanded text internally when possible, otherwise scroll its conversation."""
        units = -round(event.delta / 120 * 3)
        if not units:
            units = -1 if event.delta > 0 else 1
        if getattr(event.widget, "_flux_inner_scroll", False):
            first, last = event.widget.yview()
            if units < 0 and first > 0 or units > 0 and last < 1:
                return None
        self.transcript.yview_scroll(units, "units")
        return "break"

    def _content_columns(self, *, code: bool = False) -> int:
        """Bound embedded selectable text to the actual available conversation width."""
        pixels = self.transcript.winfo_width()
        if pixels < 100:
            pixels = max(650, self.root.winfo_width() - 340)
        points = TEXT_SIZES[self._presentation["text_size"]] * getattr(self, "_zoom_percent", 100) / 100
        average = max(7, round(points * float(self.root.tk.call("tk", "scaling")) * (0.6 if code else 0.55)))
        return max(24, min(76 if code else 64, (pixels - 64) // average))

    def _user_bubble(self, text: str, key: str, view: ConversationView) -> None:
        """Place bounded selectable user text in a rounded right-aligned expandable bubble."""
        frame = ttk.Frame(self.transcript, style="Surface.TFrame", padding=(14, 10))
        width = self._content_columns()
        full = text[:DISPLAY_TEXT_LIMIT]
        long = len(full) > 800 or full.count("\n") > 8
        display = full if key in view.expanded_content or not long else "\n".join(full[:800].split("\n")[:8]) + "…"
        height = min(12, max(1, sum(max(1, (len(line) + width - 1) // width) for line in display.splitlines())))
        content = tk.Text(frame, width=width, height=height, wrap="word", background=COLORS["surface"], highlightthickness=0, takefocus=True)
        content.pack(fill=tk.X)
        content.insert("1.0", display)
        content.configure(state="disabled")
        self._track_reflow(content, "bubble", display, 12)
        if long:
            self._expansion_button(frame, key, view)
        if key in view.expanded_content:
            content._flux_inner_scroll = True
            scroll = ttk.Scrollbar(frame, command=content.yview)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            content.configure(yscrollcommand=scroll.set)
        if len(text) > DISPLAY_TEXT_LIMIT:
            ttk.Label(frame, text="Message display capped at 12,000 characters", style="Surface.TLabel").pack(anchor="w")
        self._embed(frame, "user")

    def _working_section(self, view: ConversationView, turn_id: str, rows: list[tuple[str, str]]) -> None:
        """Render one collapsed Working section with independently expandable safe details."""
        expanded = turn_id in view.expanded_working
        count = sum(text.startswith("Result ·") for _, text in rows)
        frame = ttk.Frame(self.transcript, style="Surface.TFrame", padding=8)
        def toggle() -> None:
            """Keep the user's Working expansion through later streamed events."""
            if turn_id in view.expanded_working:
                view.expanded_working.remove(turn_id)
            else:
                view.expanded_working.add(turn_id)
            self._render_conversation()
        label = ("⌄" if expanded else "›") + f"  Working · {count} completed operations"
        if view.active_turn_id == turn_id:
            label += " · In progress"
        ttk.Button(frame, text=label, style="Accent.TButton" if view.active_turn_id == turn_id else "Success.TButton", command=toggle).pack(anchor="w")
        if expanded:
            for index, (tag, text) in enumerate(rows):
                identity = (turn_id, index)
                if tag == "code":
                    def detail(identity: tuple[str, int] = identity) -> None:
                        """Toggle one safe structured result without exposing raw payloads."""
                        if identity in view.expanded_operations:
                            view.expanded_operations.remove(identity)
                        else:
                            view.expanded_operations.add(identity)
                        self._render_conversation()
                    ttk.Button(frame, text=("⌄ " if identity in view.expanded_operations else "› ") + text.splitlines()[0].replace("_", " "), command=detail).pack(anchor="w", pady=4)
                    if identity in view.expanded_operations:
                        info = tk.Text(frame, width=self._content_columns(code=True), height=min(12, text.count("\n") + 1), background=COLORS["surface"], font="TkFixedFont", wrap="word")
                        info.pack(fill=tk.X)
                        info.insert("1.0", text[:DISPLAY_TEXT_LIMIT])
                        info.configure(state="disabled")
                        self._track_reflow(info, "info", text[:DISPLAY_TEXT_LIMIT], 12)
                else:
                    ttk.Label(frame, text=text.strip().replace("_", " "), style="Surface.TLabel").pack(anchor="w")
        self._embed(frame, "status")

    def _markdown(self, text: str, identity: str, view: ConversationView) -> None:
        """Render Markdown with bounded expansion and inert resources and HTML."""
        logo = tk.Canvas(self.transcript, width=22, height=22, background=COLORS["canvas"], highlightthickness=0)
        draw_mark(logo, 22)
        self._embed(logo, "assistant", newline=False)
        for index, block in enumerate(markdown_blocks(text)):
            key = f"{identity}:{index}"
            full = block.text[:DISPLAY_TEXT_LIMIT]
            limit = 1200 if block.kind == "code" else 2400
            long = len(full) > limit or full.count("\n") > 18
            display = full if key in view.expanded_content else "\n".join(full[:limit].split("\n")[:18]) if long else full
            if block.kind == "code":
                frame = ttk.Frame(self.transcript, style="Surface.TFrame", padding=12)
                bar = ttk.Frame(frame, style="Inset.TFrame")
                bar.pack(fill=tk.X)
                ttk.Label(bar, text=block.language or "Code", style="Surface.TLabel").pack(side=tk.LEFT)
                ttk.Button(bar, text="Copy", style="Inset.TButton", command=lambda full=full: self._copy_text(full)).pack(side=tk.RIGHT)
                code = tk.Text(frame, width=self._content_columns(code=True), height=min(18, max(2, display.count("\n") + 1)),
                               wrap="none", background=COLORS["surface"], font="TkFixedFont", highlightthickness=0)
                code.pack(fill=tk.X, pady=8)
                code.insert("1.0", display)
                code.configure(state="disabled")
                self._track_reflow(code, "code", display, 18)
                horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=code.xview)
                horizontal.pack(fill=tk.X)
                code.configure(xscrollcommand=horizontal.set)
                if key in view.expanded_content:
                    code._flux_inner_scroll = True
                    vertical = ttk.Scrollbar(frame, command=code.yview)
                    vertical.pack(side=tk.RIGHT, fill=tk.Y)
                    code.configure(yscrollcommand=vertical.set)
                if long:
                    self._expansion_button(frame, key, view)
                if len(block.text) > DISPLAY_TEXT_LIMIT:
                    ttk.Label(frame, text="Display and copy capped at 12,000 characters", style="Surface.TLabel").pack(anchor="w")
                self._embed(frame)
            else:
                self._inline_markdown(display, "heading" if block.kind == "heading" else "code" if block.kind == "table" else "assistant")
                if long:
                    button = ttk.Button(self.transcript, text="Show less" if key in view.expanded_content else "Show more", command=lambda key=key: self._toggle_content(key, view))
                    self._embed(button)
                if len(block.text) > DISPLAY_TEXT_LIMIT:
                    self.transcript.insert(tk.END, "\nDisplay capped at 12,000 characters.\n", "status")
        self.transcript.insert(tk.END, "\n")

    def _inline_markdown(self, text: str, tag: str) -> None:
        """Style inline emphasis and validated links without rendering embedded HTML."""
        pattern = re.compile(r"\[([^]\n]+)\]\(([^)\n]+)\)|\*\*(.+?)\*\*|`([^`]+)`|\*([^*\n]+)\*")
        end = 0
        for match in pattern.finditer(text):
            self.transcript.insert(tk.END, text[end:match.start()], tag)
            label, target, bold, code, italic = match.groups()
            if target:
                self._link_sequence += 1
                link_tag = "link" + str(self._link_sequence)
                self.transcript.tag_configure(link_tag, foreground=COLORS["accent"], underline=True)
                self.transcript.tag_bind(link_tag, "<Button-1>", lambda _event, target=target: self._open_link(target))
                self.transcript.insert(tk.END, label, (tag, link_tag))
                button = ttk.Button(self.transcript, text="Show file location" if target.startswith("file:") or Path(target).is_absolute() else "Open link", command=lambda target=target: self._open_link(target))
                self._embed(button)
            else:
                self.transcript.insert(tk.END, bold or code or italic, (tag, "bold" if bold else "code" if code else "italic"))
            end = match.end()
        self.transcript.insert(tk.END, text[end:], tag)

    def _open_link(self, target: str) -> None:
        """Open a web link or explicitly reveal a local file, reporting missing targets."""
        try:
            open_link(target, reveal_file=target.startswith("file:") or Path(target).is_absolute())
        except (ValueError, OSError) as error:
            self.status_text.set(str(error))

    def _copy_text(self, text: str) -> None:
        """Copy only the bounded content already permitted in the conversation display."""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_text.set("Code copied")

    def _expansion_button(self, frame: ttk.Frame, key: str, view: ConversationView) -> None:
        """Add the shared bounded code expansion action."""
        ttk.Button(frame, text="Show less" if key in view.expanded_content else "Show more", style="Inset.TButton", command=lambda: self._toggle_content(key, view)).pack(anchor="w", pady=4)

    def _toggle_content(self, key: str, view: ConversationView) -> None:
        """Keep long-content expansion stable through subsequent chunks and completion."""
        if key in view.expanded_content:
            view.expanded_content.remove(key)
        else:
            view.expanded_content.add(key)
        self._render_conversation()

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
        if self._selected_archived or conversation_id in self._skill_changing or not view.history_loaded or view.sending or view.transport_active or view.active_turn_id:
            return
        from_composer = message is None
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
        self._drafts[conversation_id] = "" if from_composer else self.message_input.get("1.0", "end-1c")
        self._draft_dirty.discard(conversation_id)
        view.sending = True
        view.transport_active = True
        view.pending_message = message
        if from_composer:
            self.message_input.delete("1.0", tk.END)
        self._render_conversation()

        def worker() -> None:
            """Queue streamed events in order without touching Tk from a worker."""
            try:
                self._write_draft(conversation_id, self._drafts.get(conversation_id, ""))
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
        if accepted and event["type"] == "turn_started":
            self._reload_sessions()
        if accepted and event["type"] == "permission_request":
            self._show_permission(conversation_id, event)
        elif event["type"] == "permission_receipt":
            pending = self._permissions.pop(event.get("permission_request_id"), None)
            self._permission_queue.pop(event.get("permission_request_id"), None)
            self._decided_permissions.add(event.get("permission_request_id"))
            if pending:
                pending[2].dismiss()
                self._showing_permission = False
            self.root.after_idle(self._present_next_permission)
            self._permission_notice()
        elif event["type"] in {"completion", "interruption", "failure"}:
            self._dismiss_permissions(conversation_id)
            if event["type"] == "completion":
                self._reload_sessions()
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
        """Queue a redacted decision without changing the current chat or its deadline."""
        request_id = event.get("permission_request_id")
        if (not isinstance(request_id, str) or request_id in self._permissions
                or request_id in self._permission_queue or request_id in self._decided_permissions or self._closing):
            return
        view = self.views.get(conversation_id)
        if not view or view.active_turn_id != event.get("turn_id") or view.error:
            return
        expiry = event.get("expires_at", time.time() + 300)
        if type(expiry) not in {int, float}:
            return
        self._permission_queue[request_id] = (conversation_id, event, expiry)
        self._present_next_permission()
        self.root.after(1000, lambda: self._load_history(conversation_id))

    def _present_next_permission(self) -> None:
        """Present a single nonexpired request and remove cancelled or resolved queue entries."""
        if self._showing_permission or self._closing:
            return
        for request_id, (owner, event, expiry) in list(self._permission_queue.items()):
            view = self.views.get(owner)
            if expiry <= time.time() or not view or view.error or view.active_turn_id != event["turn_id"]:
                self._permission_queue.pop(request_id, None)
                self._decided_permissions.add(request_id)
                continue
            if any(isinstance(child, (tk.Toplevel, InlineDecision)) and child.winfo_exists() for child in self.root.winfo_children()):
                self.root.after(250, self._present_next_permission)
                return
            self._permission_queue.pop(request_id)
            self._showing_permission = True
            metadata = self._skill_metadata.get(owner, {})
            event = {**event, "session_title": metadata.get("title", owner)}
            def decide(decision: str, owner: str = owner, event: dict = event, request_id: str = request_id, expiry: float = expiry) -> None:
                """Resolve only the captured live permission; never retry uncertain approval."""
                self._permissions.pop(request_id, None)
                self._decided_permissions.add(request_id)
                self._showing_permission = False
                if expiry <= time.time():
                    decision = "deny"
                def work() -> None:
                    """Deliver the bound decision and cancel its turn if the connection fails."""
                    try:
                        self.client.resolve_permission(request_id, decision)
                    except DesktopApiError as error:
                        if error.code != "permission_not_found":
                            try:
                                self.client.cancel_turn(owner, event["turn_id"])
                            except DesktopApiError:
                                pass
                            raise
                self._run_task(work)
                self.root.after_idle(self._present_next_permission)
                self._permission_notice()
            dialog = PermissionView(self.root, event, decide)
            self._permissions[request_id] = (owner, event["turn_id"], dialog)
            self.root.after(max(1, round((expiry - time.time()) * 1000)), lambda request_id=request_id: self._expire_permission(request_id))
            break
        self._permission_notice()

    def _expire_permission(self, request_id: str) -> None:
        """Deny an unanswered view at its original backend deadline."""
        pending = self._permissions.get(request_id)
        if pending:
            pending[2].confirm("deny")
        self._permission_queue.pop(request_id, None)
        self._permission_notice()

    def _permission_notice(self) -> None:
        """Show in-app badges for permission requests owned by other sessions."""
        pending = [owner for owner, _, _ in self._permissions.values()] + [owner for owner, _, _ in self._permission_queue.values()]
        others = sum(owner != self.active_conversation_id for owner in pending)
        if others:
            self.notice_text.set(f"{others} other session(s) waiting for permission · Review")
            self.notice_button.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        else:
            self.notice_button.grid_remove()
        self._replace_conversations(self.conversations, select=False)

    def _dismiss_permissions(self, conversation_id: str | None = None, *, deny: bool = False) -> None:
        """Remove matching visible and queued decisions without extending their lifetime."""
        for request_id, (owner, _, dialog) in list(self._permissions.items()):
            if conversation_id is None or owner == conversation_id:
                if deny:
                    dialog.confirm("deny")
                else:
                    dialog.dismiss()
                    self._showing_permission = False
                self._permissions.pop(request_id, None)
        for request_id, (owner, event, _) in list(self._permission_queue.items()):
            if conversation_id is None or owner == conversation_id:
                self._permission_queue.pop(request_id, None)
                self._decided_permissions.add(request_id)
                if deny:
                    self._run_task(lambda request_id=request_id: self.client.resolve_permission(request_id, "deny"))
        self.root.after_idle(self._present_next_permission)
        self._permission_notice()

    def open_settings(self) -> None:
        """Load local preferences and durable scopes without blocking Tk."""
        if any(isinstance(child, (tk.Toplevel, InlineDecision)) for child in self.root.winfo_children()):
            self.status_text.set("Close the current decision view before opening Settings.")
            return
        self._run_task(lambda: (self.client.settings(), self.client.list_grants()), self._settings_loaded)

    def _settings_loaded(self, result: tuple[dict[str, Any], list[dict[str, Any]]]) -> None:
        """Build a local settings view with exact, individually revocable grants."""
        settings, grants = result
        if any(isinstance(child, (tk.Toplevel, InlineDecision)) for child in self.root.winfo_children()):
            return
        window = tk.Toplevel(self.root)
        window.title("FLUX · Local settings")
        window.transient(self.root)
        prepare_dialog(window, self.root)
        body = DialogBody(window)
        window._flux_body = body
        frame = body.frame
        ttk.Label(frame, text="Settings", style="Title.TLabel").pack(anchor="w", pady=(0, 16))
        ttk.Label(frame, text="Appearance · Charcoal").pack(anchor="w")
        text_size = tk.StringVar(value=self._presentation["text_size"].title())
        text_selector = DropdownButton(frame, text_size, lambda: ["Small", "Default", "Large"], lambda _value: appearance())
        text_selector.pack(fill=tk.X, pady=8)
        reduced = tk.BooleanVar(value=self._presentation["reduced_motion"])
        def appearance(_event: object = None) -> None:
            """Apply portable appearance immediately and save it without resetting context."""
            self._presentation["text_size"] = text_size.get().lower()
            self._presentation["reduced_motion"] = reduced.get()
            self._apply_typography()
            self._save_presentation_later()
            self._render_conversation()
        ttk.Checkbutton(frame, text="Reduced motion", variable=reduced, command=appearance).pack(anchor="w", pady=(0, 16))
        ttk.Label(frame, text="Model and backend", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        model = tk.StringVar(value=settings["default_model"])
        host = tk.StringVar(value=settings["backend_host"])
        ttk.Label(frame, text="Default model for new conversations").pack(anchor="w")
        DropdownButton(frame, model, lambda: self.models).pack(fill=tk.X)
        ttk.Label(frame, text="Ollama HTTP(S) address · Applies after restarting FLUX", wraplength=580).pack(anchor="w", pady=(12, 0))
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
        ttk.Label(frame, text="Durable grants · Until revoked · Restricted to the listed workspace", wraplength=580).pack(anchor="w")
        listing = tk.Listbox(frame, width=70, height=5, exportselection=False)
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
        window.bind("<Escape>", lambda _event: window.destroy())
        center_dialog(window, self.root)

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
            self._archived_view = False
            self.search_text.set("")
            self.archive_button.configure(text="Archived sessions")
            self._select_conversation(metadata)
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
        self.skills_button.configure(text="Skills · " + str(len(metadata.get("active_skills", []))))

    def close_skills(self) -> None:
        """Close the inline inspector and restore composer focus."""
        self._skill_panel_request = None
        if self._skill_panel:
            self._skill_panel.frame.destroy()
            self._skill_panel = None
        self._skill_container.place_forget()
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
            self._skill_panel.frame.grid(row=0, column=0, sticky="nsew")
            self._skill_container.place(in_=self.transcript, relx=0, rely=0, relwidth=1, relheight=1)
            self._skill_container.lift()
            self.jump_button.grid_remove()
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
        """Choose a directory and confirm the backend's context-reset consequences."""
        selected = filedialog.askdirectory(initialdir=self.workspace_text.get(), mustexist=True)
        if not selected:
            return
        workspace = str(Path(selected).resolve())
        if self.active_conversation_id:
            self._confirm_selection(self.active_conversation_id, "workspace", workspace)
        else:
            self.workspace = Path(workspace)
            self.workspace_text.set(workspace)

    def _model_selected(self, _event: object) -> None:
        """Confirm a model change before resetting this conversation's retained context."""
        owner = self.active_conversation_id
        if not owner:
            return
        model = self.model_text.get()
        previous = self._skill_metadata.get(owner, {}).get("model", "")
        self.model_text.set(previous)
        if model != previous:
            self._confirm_selection(owner, "model", model)

    def _confirm_selection(self, owner: str, field: str, value: str) -> None:
        """Explain context reset in a temporary keyboard decision bound to its original chat."""
        if any(isinstance(child, (tk.Toplevel, InlineDecision)) for child in self.root.winfo_children()):
            self.status_text.set("Close the current decision view before changing selections.")
            return
        def decided(choice: str) -> None:
            """Apply only explicitly confirmed selections through the existing backend rule."""
            if choice != "change":
                return
            def selected(metadata: dict[str, Any]) -> None:
                """Refresh the captured chat without redirecting navigation."""
                self._skill_metadata[owner] = metadata
                if self.active_conversation_id == owner:
                    self._select_conversation(metadata)
            self._run_task(lambda: self.client.update_conversation(owner, field, value), selected)
        DecisionDialog(self.root, f"Change {field}?", f"Changing {field} resets the model context. Saved turn history remains available. New selection: {value}",
                       [("Keep current selection", "keep"), ("Change and reset context", "change")], decided)

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
        """Confirm active-work interruption before preserving drafts and stopping the service."""
        if self._closing:
            return
        if any(isinstance(child, (tk.Toplevel, InlineDecision)) for child in self.root.winfo_children()) and not self._permissions:
            self.status_text.set("Close the current decision view before exiting FLUX.")
            return
        if any(view.sending or view.transport_active or view.active_turn_id for view in self.views.values()):
            for request_id, (owner, _, dialog) in list(self._permissions.items()):
                event = next((event for event in self.views[owner].events.values() if event.get("permission_request_id") == request_id and event["type"] == "permission_request"), None)
                if event:
                    self._permission_queue[request_id] = (owner, event, event.get("expires_at", time.time() + 300))
                dialog.dismiss()
                self._permissions.pop(request_id, None)
            self._showing_permission = False
            DecisionDialog(self.root, "Work is still active", "Keep FLUX open, or request cancellation and exit. Interrupted outcomes may be uncertain.",
                           [("Keep working", "keep"), ("Stop work and exit", "stop")],
                           lambda choice: self._close_confirmed() if choice == "stop" else self._present_next_permission())
            return
        self._close_confirmed()

    def _close_confirmed(self) -> None:
        """Deny pending requests and cooperatively stop active work before backend exit."""
        if self._closing:
            return
        if self.active_conversation_id:
            self._drafts[self.active_conversation_id] = self.message_input.get("1.0", "end-1c")
        if any(len(text) > 16000 for text in self._drafts.values()):
            self.status_text.set("Shorten drafts to 16,000 characters before exiting so they can be saved.")
            return
        self._dismiss_permissions(deny=True)
        if self._skill_panel:
            self.close_skills()
        self._closing = True
        self.root.withdraw()

        def shutdown() -> None:
            """Save local presentation state and stop work, retaining the window on save failure."""
            try:
                for owner, view in list(self.views.items()):
                    if view.active_turn_id:
                        try:
                            self.client.cancel_turn(owner, view.active_turn_id)
                        except DesktopApiError as error:
                            if error.code != "turn_not_active":
                                raise
                for owner, text in list(self._drafts.items()):
                    self._write_draft(owner, text)
                if self._presentation_ready:
                    self.client.save_presentation(json.loads(json.dumps(self._presentation)))
            except (DesktopApiError, OSError, ValueError) as error:
                self._shutdown_error = error
                return
            try:
                self.stop_backend()
            finally:
                self._backend_stopped.set()

        Thread(target=shutdown, name="flux-desktop-shutdown", daemon=True).start()
        self.root.after(25, self._finish_close)

    def _exit_without_saving(self) -> None:
        """Honor an explicit discard decision while still cooperatively stopping the owned backend."""
        self._closing = True
        self.root.withdraw()
        def stop() -> None:
            """Release backend resources even when the API can no longer persist local state."""
            try:
                self.stop_backend()
            finally:
                self._backend_stopped.set()
        Thread(target=stop, name="flux-desktop-shutdown", daemon=True).start()
        self.root.after(25, self._finish_close)

    def _finish_close(self) -> None:
        """Destroy the window after the managed backend releases its resources."""
        if self._shutdown_error:
            error = self._shutdown_error
            self._shutdown_error = None
            self._closing = False
            self.root.deiconify()
            self._show_error(DesktopApiError("Exit paused because local state could not be saved. " + str(error)))
            self.root.after(25, self._drain_ui_callbacks)
            DecisionDialog(self.root, "Local changes could not be saved", "Keep the window open to recover, or exit and discard unsaved drafts and preferences.",
                           [("Keep window open", "keep"), ("Exit without saving local changes", "exit")],
                           lambda choice: self._exit_without_saving() if choice == "exit" else None)
        elif self._backend_stopped.is_set():
            self.root.destroy()
        else:
            self.root.after(25, self._finish_close)


def prepare_dialog(window: tk.Toplevel, root: tk.Tk) -> None:
    """Apply the shared charcoal dialog frame and preserve its parent's stacking level."""
    window._flux_parent_geometry = {"width": max(900, root.winfo_width()), "height": max(600, root.winfo_height()),
                                    "x": root.winfo_x(), "y": root.winfo_y()} if root.winfo_ismapped() else None
    window.configure(background=COLORS["canvas"])
    window.attributes("-topmost", root.attributes("-topmost"))
    window.bind("<Map>", lambda event: dark_dialog_frame(window) if event.widget is window else None, add=True)


def center_dialog(window: tk.Toplevel, root: tk.Tk) -> None:
    """Center a temporary view within the application and usable monitor bounds."""
    if not window.winfo_exists():
        return
    window.update_idletasks()
    saved = getattr(window, "_flux_parent_geometry", None)
    frame = getattr(root, "_flux_frame", None)
    if saved and frame and root.state() == "normal" and (root.winfo_width(), root.winfo_height()) != (saved["width"], saved["height"]):
        frame.restore_geometry(saved, display_bounds(saved["x"], saved["y"], (root.winfo_screenwidth(), root.winfo_screenheight())))
        root.update_idletasks()
    width = min(window.winfo_reqwidth(), max(600, root.winfo_width() - 80))
    height = min(window.winfo_reqheight(), max(420, root.winfo_height() - 80))
    x = root.winfo_rootx() + (root.winfo_width() - width) // 2
    y = root.winfo_rooty() + (root.winfo_height() - height) // 2
    left, top, right, bottom = display_bounds(x, y, (root.winfo_screenwidth(), root.winfo_screenheight()))
    window.geometry(f"{width}x{height}{max(left, min(x, right - width)):+d}{max(top, min(y, bottom - height)):+d}")


class DecisionDialog:
    """Use one keyboard-selection seam for temporary non-permission decisions."""

    def __init__(self, root: tk.Tk, title: str, summary: str, choices: list[tuple[str, str]], decide: Callable[[str], None]) -> None:
        """Build a centered decision whose dismissal leaves the current state unchanged."""
        self.window = InlineDecision(root)
        self.window._flux_decision = self
        self.decide = decide
        self.choices = choices
        frame = ttk.Frame(self.window, padding=24)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=title, style="Title.TLabel", wraplength=560).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text=summary, wraplength=480).pack(anchor="w")
        self.options = tk.Listbox(frame, width=58, height=len(choices), exportselection=False)
        self.options.pack(fill=tk.X, pady=16)
        for label, _value in choices:
            self.options.insert(tk.END, label)
        self.options.selection_set(0)
        self.options.bind("<Up>", lambda _event: self.move(-1))
        self.options.bind("<Down>", lambda _event: self.move(1))
        self.window.bind("<space>", lambda _event: self.choose_option())
        self.window.bind("<Return>", lambda _event: self.choose_option())
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        ttk.Button(frame, text="Confirm selection", command=self.choose_option).pack(anchor="e")
        self.window.activate()
        self.options.focus_set()

    def move(self, step: int) -> str:
        """Move the selection without committing an action."""
        selection = self.options.curselection()
        index = max(0, min(len(self.choices) - 1, (selection[0] if selection else 0) + step))
        self.options.selection_clear(0, tk.END)
        self.options.selection_set(index)
        self.options.activate(index)
        return "break"

    def choose_option(self) -> str:
        """Close the transient decision before executing the selected action."""
        selection = self.options.curselection()
        if selection:
            value = self.choices[selection[0]][1]
            self.window.destroy()
            self.decide(value)
        return "break"


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
