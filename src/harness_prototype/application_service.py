"""Versioned localhost API for presentation-independent FLUX clients."""

from collections import defaultdict, deque
from collections.abc import Callable
import hashlib
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from ollama import Client

from .access import AccessChoice
from .agent_loop import AgentRuntime
from .approval import ApprovalPromptResult, ApprovalRequest
from .application_state import ConversationStore, DurableGrantStore, StateStore
from .config import COMMAND_ALLOWED_EXECUTABLES, HOST, MODELS, SERVICES, STATE_DATABASE_PATH
from .files import file_change_target_paths
from .services import stop_all_services
from .tools import TOOL_SPECS

API_VERSION = "v1"
MAX_REQUEST_BYTES = 64 * 1024
PERMISSION_TIMEOUT_SECONDS = 300


class ProtocolError(ValueError):
    """Represent one recoverable client protocol failure."""

    def __init__(self, code: str, message: str, status: HTTPStatus) -> None:
        """Store a stable error code, safe message, and HTTP status."""
        super().__init__(message)
        self.code = code
        self.status = status


def _durable_tool_identities(
    request: ApprovalRequest, workspace: Path,
) -> list[tuple[str, str, str]]:
    """Return non-sensitive exact identities eligible for scoped reuse."""
    canonical_workspace = str(workspace.resolve())
    if request.tool_name == "run_command" and isinstance(request.arguments, dict):
        command = request.arguments.get("command")
        if isinstance(command, list) and command and isinstance(command[0], str):
            return [("executable", canonical_workspace, command[0].casefold())]
    if request.tool_name == "apply_file_changes":
        targets = file_change_target_paths(request.arguments)
        if isinstance(targets, list) and targets:
            return [("file", canonical_workspace, str(path.resolve())) for path in targets]
    if request.tool_name == "start_service" and isinstance(request.arguments, dict):
        name = request.arguments.get("name")
        if isinstance(name, str):
            return [("service", canonical_workspace, name)]
    if request.tool_name == "fetch_documentation":
        return [("risk_category", canonical_workspace, request.category.value)]
    try:
        encoded = json.dumps(
            request.arguments, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return []
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return [("operation", canonical_workspace, f"{request.tool_name}:sha256:{fingerprint}")]


def _tool_identity_is_valid(request: ApprovalRequest) -> bool:
    """Revalidate configuration properties that define a reusable tool grant."""
    if request.tool_name == "run_command":
        identities = _durable_tool_identities(request, Path.cwd())
        return bool(identities) and identities[0][2] in COMMAND_ALLOWED_EXECUTABLES
    if request.tool_name == "start_service" and isinstance(request.arguments, dict):
        return request.arguments.get("name") in SERVICES
    return True


class PermissionBroker:
    """Coordinate pending permission decisions without exposing raw payloads."""

    def __init__(self) -> None:
        """Create an empty thread-safe request registry."""
        self._lock = Lock()
        self._pending: dict[str, tuple[Event, str | None]] = {}

    def request(
        self,
        event: dict[str, Any],
        emit: Callable[[str, dict[str, Any]], None],
        cancelled: Callable[[], bool],
    ) -> str | None:
        """Publish a request and wait boundedly for its matching decision."""
        request_id = str(uuid4())
        ready = Event()
        with self._lock:
            self._pending[request_id] = (ready, None)
        emit("permission_request", {"permission_request_id": request_id, **event})
        deadline = monotonic() + PERMISSION_TIMEOUT_SECONDS
        while not ready.wait(0.1):
            if cancelled() or monotonic() >= deadline:
                break
        with self._lock:
            pending = self._pending.pop(request_id, None)
        return pending[1] if pending else None

    def resolve(self, request_id: str, decision: str) -> bool:
        """Resolve one pending permission exactly once."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            self._pending[request_id] = (pending[0], decision)
            pending[0].set()
            return True


class ConversationRegistry:
    """Own active in-memory runtimes and their permission broker."""

    def __init__(
        self,
        runtime_factory: Callable[[str, Path], AgentRuntime],
        models: tuple[str, ...],
        store: ConversationStore,
        grants: DurableGrantStore,
    ) -> None:
        """Create a registry with injectable runtime construction."""
        self.runtime_factory = runtime_factory
        self.models = models
        self.store = store
        self.grants = grants
        self.permissions = PermissionBroker()
        self.session_directory_grants: set[tuple[str, str]] = set()
        self.session_tool_grants: set[tuple[str, str, str]] = set()
        self._lock = Lock()
        self._turns_changed = Condition(self._lock)
        self._runtimes: dict[str, AgentRuntime] = {}
        self._active_turns: dict[str, tuple[str, AgentRuntime]] = {}
        self._shutting_down = False

    def create(self, model: str, workspace: Path) -> AgentRuntime:
        """Create and retain one conversation runtime."""
        if not self.models:
            raise ProtocolError(
                "model_configuration_required",
                "No models are configured. Set HARNESS_MODELS to one or more installed "
                "Ollama model identifiers, then restart FLUX.",
                HTTPStatus.CONFLICT,
            )
        if model not in self.models:
            raise ProtocolError("invalid_model", "Model is not configured.", HTTPStatus.BAD_REQUEST)
        resolved = workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise ProtocolError(
                "invalid_workspace", "Workspace must be an existing directory.", HTTPStatus.BAD_REQUEST
            )
        runtime = self.runtime_factory(model, resolved)
        self.store.create(runtime.conversation_id, "New conversation", model, resolved, runtime.messages)
        with self._lock:
            self._runtimes[runtime.conversation_id] = runtime
        return runtime

    def get(self, conversation_id: str) -> AgentRuntime:
        """Return one conversation or a structured not-found error."""
        with self._lock:
            runtime = self._runtimes.get(conversation_id)
        if runtime is None:
            persisted = self.store.load(conversation_id)
            if persisted is not None:
                workspace = Path(persisted["workspace"])
                if not workspace.is_dir():
                    raise ProtocolError(
                        "workspace_unavailable",
                        "The conversation workspace is no longer available.",
                        HTTPStatus.CONFLICT,
                    )
                runtime = self.runtime_factory(persisted["model"], workspace)
                runtime.conversation_id = persisted["conversation_id"]
                runtime.messages = persisted["messages"]
                runtime.capability_state.active_groups = set(persisted["capability_groups"])
                if persisted["active_skills"]:
                    activation = runtime.activate_skills(persisted["active_skills"])
                    if not activation["ok"]:
                        runtime.skill_state.active_ids = ()
                with self._lock:
                    self._runtimes[conversation_id] = runtime
        if runtime is None:
            raise ProtocolError(
                "conversation_not_found", "Conversation was not found.", HTTPStatus.NOT_FOUND
            )
        return runtime

    def list(self, limit: int, after: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Return one stable page of persisted conversation metadata."""
        try:
            return self.store.list_conversations(limit, after)
        except ValueError as error:
            raise ProtocolError("invalid_cursor", str(error), HTTPStatus.BAD_REQUEST) from error

    def rename(self, conversation_id: str, title: str) -> AgentRuntime:
        """Rename an idle conversation without changing its retained context."""
        runtime = self.get_idle(conversation_id)
        self.store.update_metadata(conversation_id, title=title)
        return runtime

    def delete(self, conversation_id: str) -> None:
        """Delete an idle persisted conversation and evict its runtime."""
        self.get_idle(conversation_id)
        if not self.store.delete(conversation_id):
            raise ProtocolError(
                "conversation_not_found", "Conversation was not found.", HTTPStatus.NOT_FOUND
            )
        with self._lock:
            self._runtimes.pop(conversation_id, None)

    def begin_turn(self, conversation_id: str, turn_id: str) -> AgentRuntime:
        """Prepare and reserve one conversation for an identified turn."""
        runtime = self.get(conversation_id)
        with self._lock:
            if self._shutting_down:
                raise ProtocolError(
                    "service_stopping", "The application service is stopping.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            if conversation_id in self._active_turns:
                raise ProtocolError(
                    "conversation_busy",
                    "Conversation already has an active turn.",
                    HTTPStatus.CONFLICT,
                )
            runtime.prepare_turn()
            self._active_turns[conversation_id] = (turn_id, runtime)
        return runtime

    def abandon_turn(self, conversation_id: str) -> None:
        """Release a reservation when persistence fails before execution starts."""
        self.finish_turn(conversation_id)

    def cancel_turn(self, conversation_id: str, turn_id: str) -> None:
        """Interrupt only the active turn matching both stable identifiers."""
        self.get(conversation_id)
        with self._lock:
            active = self._active_turns.get(conversation_id)
            if active is None or active[0] != turn_id:
                raise ProtocolError(
                    "turn_not_active", "The identified turn is not active.", HTTPStatus.NOT_FOUND
                )
            active[1].interrupt()

    def get_idle(self, conversation_id: str) -> AgentRuntime:
        """Return a runtime only when selection changes cannot race a turn."""
        runtime = self.get(conversation_id)
        with self._lock:
            if conversation_id in self._active_turns:
                raise ProtocolError(
                    "conversation_busy",
                    "Conversation already has an active turn.",
                    HTTPStatus.CONFLICT,
                )
        return runtime

    def finish_turn(self, conversation_id: str) -> None:
        """Release an active turn reservation after every outcome."""
        with self._lock:
            self._active_turns.pop(conversation_id, None)
            self._turns_changed.notify_all()

    def stop(self, timeout: float = 5) -> None:
        """Interrupt active turns and wait boundedly for their cleanup."""
        deadline = monotonic() + timeout
        with self._lock:
            self._shutting_down = True
            for _, runtime in self._active_turns.values():
                runtime.interrupt()
            while self._active_turns:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._turns_changed.wait(remaining)


class TurnEventStream:
    """Write ordered portable turn events as newline-delimited JSON."""

    def __init__(
        self,
        handler: BaseHTTPRequestHandler,
        runtime: AgentRuntime,
        turn_id: str,
    ) -> None:
        """Bind one event sequence to a response and active runtime."""
        self.handler = handler
        self.runtime = runtime
        self.turn_id = turn_id
        self.sequence = 0
        self.disconnected = False
        self.tool_ids: dict[str, deque[str]] = defaultdict(deque)

    def emit(self, event_type: str, fields: dict[str, Any] | None = None) -> None:
        """Write and flush one ordered event, cancelling after disconnect."""
        if self.disconnected:
            return
        self.sequence += 1
        event = {
            "api_version": API_VERSION,
            "conversation_id": self.runtime.conversation_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "type": event_type,
            **(fields or {}),
        }
        try:
            self.handler.wfile.write(
                (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            )
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.disconnected = True
            self.runtime.interrupt()

    def observe_runtime(self, event: dict[str, Any]) -> None:
        """Translate runtime log events into the public event contract."""
        event_type = event.get("type")
        if event_type == "tool_call":
            tool_call_id = str(uuid4())
            name = str(event.get("name", "unknown"))
            self.tool_ids[name].append(tool_call_id)
            self.emit("tool_call", {"tool_call_id": tool_call_id, "name": name})
        elif event_type == "tool_result":
            name = str(event.get("name", "unknown"))
            tool_call_id = self.tool_ids[name].popleft() if self.tool_ids[name] else str(uuid4())
            self.emit(
                "tool_result",
                {"tool_call_id": tool_call_id, "name": name, "result": event.get("result")},
            )


class ApplicationServiceHandler(BaseHTTPRequestHandler):
    """Serve the version-one application protocol without browser UI."""

    server: "ApplicationHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Leave host logging policy to the desktop application."""

    def do_GET(self) -> None:
        """Return health, capabilities, or conversation metadata."""
        try:
            parts = self._path_parts()
            if parts == [API_VERSION, "health"]:
                self._json(HTTPStatus.OK, {"api_version": API_VERSION, "status": "ready"})
            elif parts == [API_VERSION, "capabilities"]:
                self._json(HTTPStatus.OK, self._capabilities())
            elif parts == [API_VERSION, "conversations"]:
                limit, after = self._paging_query(text_cursor=True)
                conversations, next_cursor = self.server.registry.list(limit, after)
                self._json(HTTPStatus.OK, {"api_version": API_VERSION, "conversations": conversations, "next_cursor": next_cursor})
            elif parts == [API_VERSION, "grants"]:
                self._json(HTTPStatus.OK, {
                    "api_version": API_VERSION,
                    "grants": self.server.registry.grants.list(),
                })
            elif len(parts) == 4 and parts[:2] == [API_VERSION, "conversations"] and parts[3] == "history":
                self.server.registry.get(parts[2])
                limit, after = self._paging_query(text_cursor=False)
                turns, next_cursor = self.server.registry.store.history(parts[2], limit, int(after or 0))
                self._json(HTTPStatus.OK, {"api_version": API_VERSION, "conversation_id": parts[2], "turns": turns, "next_cursor": next_cursor})
            elif len(parts) == 3 and parts[:2] == [API_VERSION, "conversations"]:
                runtime = self.server.registry.get(parts[2])
                self._json(HTTPStatus.OK, self._conversation(runtime))
            else:
                raise ProtocolError("not_found", "Operation was not found.", HTTPStatus.NOT_FOUND)
        except ProtocolError as error:
            self._protocol_error(error)

    def do_POST(self) -> None:
        """Create conversations, run turns, cancel turns, or resolve permissions."""
        try:
            parts = self._path_parts()
            body = self._read_json()
            if parts == [API_VERSION, "conversations"]:
                self._create_conversation(body)
            elif len(parts) == 4 and parts[:2] == [API_VERSION, "conversations"] and parts[3] == "turns":
                self._run_turn(parts[2], body)
            elif (
                len(parts) == 6
                and parts[:2] == [API_VERSION, "conversations"]
                and parts[3] == "turns"
                and parts[5] == "cancel"
            ):
                self.server.registry.cancel_turn(parts[2], parts[4])
                self._json(
                    HTTPStatus.ACCEPTED,
                    {
                        "status": "cancellation_requested",
                        "conversation_id": parts[2],
                        "turn_id": parts[4],
                    },
                )
            elif len(parts) == 3 and parts[:2] == [API_VERSION, "permissions"]:
                self._resolve_permission(parts[2], body)
            else:
                raise ProtocolError("not_found", "Operation was not found.", HTTPStatus.NOT_FOUND)
        except ProtocolError as error:
            self._protocol_error(error)

    def do_PATCH(self) -> None:
        """Rename or select a configured model or workspace for a conversation."""
        try:
            parts = self._path_parts()
            if len(parts) != 3 or parts[:2] != [API_VERSION, "conversations"]:
                raise ProtocolError("not_found", "Operation was not found.", HTTPStatus.NOT_FOUND)
            body = self._read_json()
            if set(body) == {"title"} and isinstance(body["title"], str):
                title = body["title"].strip()
                if not title or len(title) > 200:
                    raise ProtocolError("invalid_title", "Title must be 1 to 200 characters.", HTTPStatus.BAD_REQUEST)
                runtime = self.server.registry.rename(parts[2], title)
            else:
                runtime = self.server.registry.get_idle(parts[2])
            if set(body) == {"model"} and isinstance(body["model"], str):
                if body["model"] not in self.server.registry.models:
                    raise ProtocolError("invalid_model", "Model is not configured.", HTTPStatus.BAD_REQUEST)
                runtime.change_model(body["model"])
                self.server.registry.store.save_runtime(runtime)
            elif set(body) == {"workspace"} and isinstance(body["workspace"], str):
                workspace = Path(body["workspace"]).expanduser().resolve()
                if not workspace.is_dir():
                    raise ProtocolError("invalid_workspace", "Workspace must be an existing directory.", HTTPStatus.BAD_REQUEST)
                runtime.change_workspace(workspace)
                self.server.registry.store.save_runtime(runtime)
            elif set(body) != {"title"}:
                raise ProtocolError("invalid_request", "Expected one model or workspace string.", HTTPStatus.BAD_REQUEST)
            self._json(HTTPStatus.OK, self._conversation(runtime))
        except ProtocolError as error:
            self._protocol_error(error)

    def do_DELETE(self) -> None:
        """Delete one conversation or revoke one durable permission grant."""
        try:
            parts = self._path_parts()
            if len(parts) == 3 and parts[:2] == [API_VERSION, "grants"]:
                if not self.server.registry.grants.revoke(parts[2]):
                    raise ProtocolError(
                        "grant_not_found", "Durable grant was not found.", HTTPStatus.NOT_FOUND
                    )
                self._json(HTTPStatus.OK, {"status": "revoked", "grant_id": parts[2]})
                return
            if len(parts) != 3 or parts[:2] != [API_VERSION, "conversations"]:
                raise ProtocolError("not_found", "Operation was not found.", HTTPStatus.NOT_FOUND)
            self.server.registry.delete(parts[2])
            self._json(HTTPStatus.OK, {"status": "deleted", "conversation_id": parts[2]})
        except ProtocolError as error:
            self._protocol_error(error)

    def _create_conversation(self, body: dict[str, Any]) -> None:
        """Validate creation input and return stable conversation metadata."""
        if set(body) - {"model", "workspace"}:
            raise ProtocolError("invalid_request", "Request contains unsupported fields.", HTTPStatus.BAD_REQUEST)
        configured_models = self.server.registry.models
        if not configured_models:
            raise ProtocolError(
                "model_configuration_required",
                "No models are configured. Set HARNESS_MODELS to one or more installed "
                "Ollama model identifiers, then restart FLUX.",
                HTTPStatus.CONFLICT,
            )
        model = body.get("model", configured_models[0])
        workspace = body.get("workspace", str(self.server.default_workspace))
        if not isinstance(model, str) or not isinstance(workspace, str):
            raise ProtocolError("invalid_request", "Model and workspace must be strings.", HTTPStatus.BAD_REQUEST)
        runtime = self.server.registry.create(model, Path(workspace))
        self._json(HTTPStatus.CREATED, self._conversation(runtime))

    def _run_turn(self, conversation_id: str, body: dict[str, Any]) -> None:
        """Run one turn and stream its events in observation order."""
        if set(body) != {"message"} or not isinstance(body["message"], str) or not body["message"].strip():
            raise ProtocolError("invalid_request", "Expected one nonempty message string.", HTTPStatus.BAD_REQUEST)
        turn_id = str(uuid4())
        runtime = self.server.registry.begin_turn(conversation_id, turn_id)
        try:
            self.server.registry.store.start_turn(conversation_id, turn_id, body["message"])
        except Exception:
            self.server.registry.abandon_turn(conversation_id)
            raise
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        stream = TurnEventStream(self, runtime, turn_id)
        stream.emit("turn_started")
        workspace = str(runtime.workspace.resolve())

        def directory_permission(path: Path) -> AccessChoice:
            """Request a scoped filesystem decision through the stream."""
            canonical = path.expanduser().resolve()
            session_match = any(
                saved_workspace == workspace
                and (canonical == Path(saved_path) or canonical.is_relative_to(Path(saved_path)))
                for saved_workspace, saved_path in self.server.registry.session_directory_grants
            )
            if session_match:
                stream.emit("permission_grant", {"action": "reused", "lifetime": "session", "kind": "directory"})
                return AccessChoice.GRANT_DIRECTORY
            for grant in self.server.registry.grants.list():
                granted_path = Path(grant["target"])
                if (
                    grant["kind"] == "directory"
                    and grant["workspace"] == workspace
                    and granted_path.is_dir()
                    and (canonical == granted_path or canonical.is_relative_to(granted_path))
                ):
                    self.server.registry.grants.match("directory", workspace, grant["target"])
                    stream.emit("permission_grant", {"action": "reused", "lifetime": "durable", "kind": "directory", "grant_id": grant["grant_id"]})
                    return AccessChoice.GRANT_ONCE
            decision = self.server.registry.permissions.request(
                {"kind": "directory", "path": str(canonical), "choices": [
                    "allow_once", "allow_session", "allow_durable", "grant_all_session", "deny"
                ]},
                stream.emit,
                lambda: stream.disconnected,
            )
            if decision == "allow_session":
                self.server.registry.session_directory_grants.add((workspace, str(canonical)))
            elif decision == "allow_durable":
                grant = self.server.registry.grants.create(
                    str(uuid4()), "directory", workspace, str(canonical)
                )
                stream.emit("permission_grant", {"action": "created", "lifetime": "durable", "kind": "directory", "grant_id": grant["grant_id"]})
            elif decision == "deny":
                self.server.registry.grants.record_denial("directory")
                stream.emit("permission_grant", {"action": "denied", "lifetime": "one_time", "kind": "directory"})
            if decision == "grant_all_session":
                return AccessChoice.GRANT_ALL
            if decision == "allow_session":
                return AccessChoice.GRANT_DIRECTORY
            if decision in {"allow_once", "allow_durable"}:
                return AccessChoice.GRANT_ONCE
            return AccessChoice.DENY

        def tool_approval(request: ApprovalRequest) -> ApprovalPromptResult:
            """Request a redacted tool-policy decision through the stream."""
            identities = _durable_tool_identities(request, runtime.workspace)
            if identities and all(identity in self.server.registry.session_tool_grants for identity in identities):
                stream.emit("permission_grant", {"action": "reused", "lifetime": "session", "kind": identities[0][0]})
                return ApprovalPromptResult(True, "session_tool_grant")
            saved_grants = {
                (grant["kind"], grant["workspace"], grant["target"]): grant
                for grant in self.server.registry.grants.list()
            }
            durable_match = (
                bool(identities)
                and _tool_identity_is_valid(request)
                and all(identity in saved_grants for identity in identities)
            )
            if durable_match:
                for kind, saved_workspace, target in identities:
                    self.server.registry.grants.match(kind, saved_workspace, target)
                stream.emit("permission_grant", {"action": "reused", "lifetime": "durable", "kind": identities[0][0]})
                return ApprovalPromptResult(True, "durable_tool_grant")
            choices = ["allow_once", "deny"]
            if identities:
                choices = ["allow_once", "allow_session", "allow_durable", "deny"]
            decision = self.server.registry.permissions.request(
                {
                    "kind": "tool",
                    "tool_name": request.tool_name,
                    "category": request.category.value,
                    "choices": choices,
                },
                stream.emit,
                lambda: stream.disconnected,
            )
            if decision == "allow_session":
                self.server.registry.session_tool_grants.update(identities)
            elif decision == "allow_durable":
                for kind, saved_workspace, target in identities:
                    self.server.registry.grants.create(
                        str(uuid4()), kind, saved_workspace, target
                    )
                stream.emit("permission_grant", {"action": "created", "lifetime": "durable", "kind": identities[0][0]})
            elif decision in {None, "deny"}:
                self.server.registry.grants.record_denial(
                    identities[0][0] if identities else "tool"
                )
                stream.emit("permission_grant", {"action": "denied", "lifetime": "one_time", "kind": "tool"})
            return ApprovalPromptResult(
                decision in {"allow_once", "allow_session", "allow_durable"},
                f"service_client_{decision or 'interrupted'}",
            )

        def observe_event(event: dict[str, Any]) -> None:
            """Persist runtime evidence before exposing its public projection."""
            if event.get("type") == "tool_result":
                self.server.registry.store.checkpoint_turn(runtime, turn_id, event)
            else:
                self.server.registry.store.save_turn_event(conversation_id, turn_id, event)
            stream.observe_runtime(event)

        try:
            result = runtime.continue_conversation(
                body["message"],
                on_text=lambda text: stream.emit("assistant_text", {"text": text}) if text else None,
                on_permission=directory_permission,
                on_approval=tool_approval,
                on_event=observe_event,
                interrupt_prepared=True,
            )
            self.server.registry.store.save_runtime(runtime)
            self.server.registry.store.save_turn_messages(
                conversation_id, turn_id, runtime.messages
            )
            self.server.registry.store.finish_turn(
                conversation_id, turn_id, result.outcome, result.error
            )
        finally:
            self.server.registry.finish_turn(conversation_id)
        if result.outcome == "cancelled":
            stream.emit("interruption", {"message": result.error})
        elif result.error:
            stream.emit("failure", {"outcome": result.outcome, "message": result.error, "recoverable": True})
        stream.emit("completion", {"outcome": result.outcome})
        self.close_connection = True

    def _resolve_permission(self, request_id: str, body: dict[str, Any]) -> None:
        """Resolve a pending permission with a documented choice string."""
        if set(body) != {"decision"} or not isinstance(body["decision"], str):
            raise ProtocolError("invalid_request", "Expected one decision string.", HTTPStatus.BAD_REQUEST)
        if body["decision"] not in {
            "allow", "allow_once", "allow_session", "allow_durable", "deny",
            "grant_directory", "grant_all", "grant_all_session",
        }:
            raise ProtocolError("invalid_decision", "Permission decision is not supported.", HTTPStatus.BAD_REQUEST)
        decision = {
            "allow": "allow_once", "grant_directory": "allow_session",
            "grant_all": "grant_all_session",
        }.get(body["decision"], body["decision"])
        if not self.server.registry.permissions.resolve(request_id, decision):
            raise ProtocolError("permission_not_found", "Permission request is no longer pending.", HTTPStatus.NOT_FOUND)
        self._json(HTTPStatus.OK, {"status": "resolved", "permission_request_id": request_id})

    def _read_json(self) -> dict[str, Any]:
        """Read one bounded JSON object request body."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            raise ProtocolError("invalid_length", "Content-Length is required.", HTTPStatus.LENGTH_REQUIRED) from None
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ProtocolError("request_too_large", "Request body exceeds the service limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProtocolError("invalid_json", "Request body must be a UTF-8 JSON object.", HTTPStatus.BAD_REQUEST) from None
        if not isinstance(body, dict):
            raise ProtocolError("invalid_request", "Request body must be a JSON object.", HTTPStatus.BAD_REQUEST)
        return body

    def _path_parts(self) -> list[str]:
        """Return decoded route segments without accepting query operations."""
        parsed = urlsplit(self.path)
        if parsed.fragment:
            raise ProtocolError("invalid_path", "Fragments are not supported.", HTTPStatus.BAD_REQUEST)
        return [part for part in parsed.path.split("/") if part]

    def _paging_query(self, *, text_cursor: bool) -> tuple[int, str | None]:
        """Validate bounded stable paging query parameters."""
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(query) - {"limit", "after"} or any(len(values) != 1 for values in query.values()):
            raise ProtocolError("invalid_query", "Only one limit and after value are supported.", HTTPStatus.BAD_REQUEST)
        try:
            limit = int(query.get("limit", ["50"])[0])
            if isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ValueError
            after = query.get("after", [None])[0]
            if after == "":
                raise ValueError
            if after is not None and not text_cursor:
                parsed_after = int(after)
                if parsed_after < 0:
                    raise ValueError
                after = str(parsed_after)
        except ValueError:
            raise ProtocolError("invalid_paging", "limit must be 1 to 100 and after must be a valid cursor.", HTTPStatus.BAD_REQUEST) from None
        return limit, after

    def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        """Write one bounded JSON response."""
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _protocol_error(self, error: ProtocolError) -> None:
        """Return the stable protocol error envelope."""
        self._json(error.status, {"error": {"code": error.code, "message": str(error), "recoverable": error.status.value < 500}})

    def _capabilities(self) -> dict[str, Any]:
        """Describe portable API limits and backend-owned capabilities."""
        return {
            "api_version": API_VERSION,
            "models": list(self.server.registry.models),
            "tools": sorted(TOOL_SPECS),
            "operations": ["health", "conversations", "conversation_history", "rename", "delete", "turns", "cancellation", "permissions", "durable_grants", "model_selection", "workspace_selection"],
            "event_types": ["turn_started", "assistant_text", "tool_call", "tool_result", "permission_request", "permission_grant", "failure", "interruption", "completion"],
            "limits": {"max_request_bytes": MAX_REQUEST_BYTES},
        }

    def _conversation(self, runtime: AgentRuntime) -> dict[str, Any]:
        """Return public conversation metadata."""
        persisted = self.server.registry.store.load(runtime.conversation_id) or {}
        return {
            "api_version": API_VERSION,
            "conversation_id": runtime.conversation_id,
            "title": persisted.get("title", "New conversation"),
            "model": runtime.model,
            "workspace": str(runtime.workspace),
            "created_at": persisted.get("created_at"),
            "updated_at": persisted.get("updated_at"),
        }


class ApplicationHTTPServer(ThreadingHTTPServer):
    """Carry application dependencies for request handlers."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], registry: ConversationRegistry, workspace: Path) -> None:
        """Bind the handler to its registry and default workspace."""
        self.registry = registry
        self.default_workspace = workspace.resolve()
        super().__init__(address, ApplicationServiceHandler)


class DesktopServiceHost:
    """Provide a platform-neutral startup, readiness, and shutdown seam."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        allow_remote: bool = False,
        workspace: Path | None = None,
        models: tuple[str, ...] = MODELS,
        runtime_factory: Callable[[str, Path], AgentRuntime] | None = None,
        state_database_path: Path = STATE_DATABASE_PATH,
    ) -> None:
        """Validate exposure policy and configure a not-yet-started service."""
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.casefold() == "localhost"
        if not local and not allow_remote:
            raise ValueError("Non-loopback application service exposure requires allow_remote=True.")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 through 65535.")
        active_workspace = (workspace or Path.cwd()).resolve()
        factory = runtime_factory or (lambda model, path: AgentRuntime(Client(host=HOST), model, path))
        state = StateStore(state_database_path)
        store = ConversationStore(state)
        self.server = ApplicationHTTPServer(
            (host, port),
            ConversationRegistry(factory, models, store, DurableGrantStore(state)),
            active_workspace,
        )
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """Return the effective listener address, including an assigned port."""
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """Start serving in a desktop-host-managed background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = Thread(target=self.server.serve_forever, name="flux-application-service", daemon=True)
        self._thread.start()

    def is_ready(self) -> bool:
        """Report whether the managed service thread is accepting work."""
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        """Stop the listener and release its socket deterministically."""
        self.server.registry.stop()
        stop_all_services()
        if self._thread and self._thread.is_alive():
            self.server.shutdown()
            self._thread.join(timeout=5)
        self.server.server_close()
