"""Native desktop shell for the loopback FLUX application service."""

from collections.abc import Callable
import json
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .application_service import DesktopServiceHost
from .application_state import StateStoreError


REQUEST_TIMEOUT_SECONDS = 5


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
        ttk.Button(controls, text="Choose workspace…", command=self.choose_workspace).grid(
            row=0, column=2
        )
        ttk.Label(controls, textvariable=self.workspace_text, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

        empty = ttk.Frame(workspace, padding=40)
        empty.grid(row=2, column=0, sticky="nsew")
        ttk.Label(
            empty,
            text="Your conversation workspace",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(80, 8))
        ttk.Label(
            empty,
            text="Turn rendering and permission decisions arrive in the next GUI milestone.",
            style="Muted.TLabel",
        ).pack()
        self.retry_button = ttk.Button(empty, text="Retry connection", command=self.refresh)

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
            while True:
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
            self.retry_button.pack_forget()
        else:
            self.status_text.set("Backend connected · Ollama unavailable")
            self.detail_text.set(ollama.get("message", "Start Ollama, then retry."))
            self.retry_button.pack(pady=20)
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
            self.conversation_list.selection_set(0)
            self._select_conversation(conversations[0])

    def _conversation_selected(self, _event: object) -> None:
        """Load the selected navigation item's backend metadata."""
        selection = self.conversation_list.curselection()
        if not selection:
            return
        conversation_id = self.conversations[selection[0]]["conversation_id"]
        self._run_task(
            lambda: self.client.get_conversation(conversation_id), self._select_conversation
        )

    def _select_conversation(self, conversation: dict[str, Any]) -> None:
        """Display one conversation's backend-owned selections."""
        self.active_conversation_id = conversation["conversation_id"]
        self.detail_text.set(conversation.get("title", "New conversation"))
        self.model_text.set(conversation.get("model", ""))
        self.workspace_text.set(conversation.get("workspace", str(self.workspace)))

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
        self.retry_button.pack(pady=20)

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
