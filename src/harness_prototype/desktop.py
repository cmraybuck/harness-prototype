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
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .application_service import DesktopServiceHost
from .application_state import StateStoreError


REQUEST_TIMEOUT_SECONDS = 5
DISPLAY_TEXT_LIMIT = 12000
STREAM_EVENT_LIMIT = 2 * 1024 * 1024


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

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return the first bounded conversation page for navigation."""
        return self._request("GET", "/conversations?limit=100")["conversations"]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return current metadata for one conversation."""
        return self._request("GET", f"/conversations/{quote(conversation_id, safe='')}")

    def create_conversation(self, model: str, workspace: Path) -> dict[str, Any]:
        """Create one backend-owned durable conversation."""
        return self._request(
            "POST", "/conversations", {"model": model, "workspace": str(workspace)}
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
                    rows.append(("status", "\nPermission required · Waiting for approval. "
                                 "Decision controls are not available in this GUI yet; "
                                 "cancel or wait for automatic denial.\n"))
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
        self._backend_stopped = Event()
        self._ui_callbacks: SimpleQueue[Callable[[], None]] = SimpleQueue()

        self.status_text = tk.StringVar(value="Starting local backend…")
        self.detail_text = tk.StringVar(value="Connecting to FLUX")
        self.model_text = tk.StringVar()
        self.workspace_text = tk.StringVar(value=str(self.workspace))
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
        ttk.Label(composer, text="Message (Ctrl+Enter to send)").grid(row=0, column=0, sticky="w")
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
    ) -> None:
        """Run backend I/O off the Tk event loop and marshal its result safely."""
        def runner() -> None:
            """Execute one operation and queue success or failure on the UI thread."""
            try:
                result = work()
            except (DesktopApiError, OSError, ValueError) as error:
                if not self._closing:
                    self._ui_callbacks.put(lambda error=error: self._show_error(error))
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
        self.active_conversation_id = conversation["conversation_id"]
        self.detail_text.set(conversation.get("title", "New conversation"))
        self.model_text.set(conversation.get("model", ""))
        self.workspace_text.set(conversation.get("workspace", str(self.workspace)))
        self.views.setdefault(self.active_conversation_id, ConversationView())
        self._render_conversation()
        self._load_history(self.active_conversation_id)

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
            else:
                view.recover(turns or [])
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
        busy = bool(view and (not view.history_loaded or view.sending or view.transport_active or view.active_turn_id))
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
        if not view.history_loaded or view.sending or view.transport_active or view.active_turn_id or view.error:
            return
        message = message if message is not None else self.message_input.get("1.0", "end-1c")
        if not message.strip():
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
            self.views[conversation_id].accept(event)
        except DesktopApiError as error:
            self._turn_error(conversation_id, error)
            return
        if self.active_conversation_id == conversation_id:
            self._render_conversation()

    def _turn_error(self, conversation_id: str, error: Exception) -> None:
        """Preserve uncertain submissions and require recovery before another send."""
        view = self.views[conversation_id]
        view.error = str(error)
        view.transport_active = False
        view.sending = False
        if self.active_conversation_id == conversation_id:
            self._render_conversation()

    def cancel_turn(self) -> None:
        """Capture both IDs so switching views cannot cancel another turn."""
        conversation_id = self.active_conversation_id
        view = self.views.get(conversation_id)
        if view and view.active_turn_id:
            turn_id = view.active_turn_id
            self._run_task(lambda: self.client.cancel_turn(conversation_id, turn_id))

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
        """Create a durable conversation using the current selections."""
        if not self.models:
            self._show_error(DesktopApiError("No Ollama models are configured."))
            return
        model = self.model_text.get() or self.models[0]
        workspace = Path(self.workspace_text.get())
        self.status_text.set("Creating conversation…")
        self._run_task(
            lambda: self.client.create_conversation(model, workspace),
            lambda _created: self.refresh(),
        )

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
        self.detail_text.set(str(error))
        self.retry_button.grid(row=4, column=0, sticky="w", pady=8)

    def close(self) -> None:
        """Close the window promptly and stop the managed backend asynchronously."""
        if self._closing:
            return
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
