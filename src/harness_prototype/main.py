"""Interactive terminal entry point."""

import os
from pathlib import Path
import shutil
import sys

from ollama import Client, ResponseError

from .access import AccessChoice, WorkspaceAccess
from .approval import ApprovalPromptResult, ApprovalRequest
from .agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    new_conversation,
    run_turn,
)
from .config import (
    HOST,
    LOG_DIRECTORY,
    LOG_REDACT_VALUES,
    MODEL,
    MODELS,
    TEST_TIMEOUT_SECONDS,
)
from .files import file_change_target_paths
from .interaction_log import InteractionLogger


MODEL_DESCRIPTIONS = {
    "devstral-small-2:latest": "more capable; slower when it does not fit fully in GPU memory",
    "qwen3:8b": "faster and lighter; may be less capable on complex tasks",
}


def _terminal_text(value: object, max_chars: int = 160) -> str:
    """Return bounded single-line text that cannot control the terminal."""
    safe = "".join(character if character.isprintable() else " " for character in str(value))
    safe = " ".join(safe.split())
    if len(safe) <= max_chars:
        return safe
    return safe[: max_chars - 3] + "..."


class TerminalUI:
    """Render the conversation with a compact, consistent terminal layout."""

    def __init__(self) -> None:
        """Enable ANSI styling only for an interactive terminal that permits it."""
        self.use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.response_open = False
        self.approved_edit_files: set[Path] = set()
        self.approved_command_executables: set[str] = set()

    def style(self, text: str, code: str) -> str:
        """Apply an ANSI style when terminal color support is available."""
        if not self.use_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def clear_screen(self) -> None:
        """Clear the visible terminal and its scrollback in interactive sessions."""
        if not sys.stdout.isatty():
            return
        # Erase the display and scrollback before returning the cursor home.
        print("\033[2J\033[3J\033[H", end="", flush=True)

    def rule(self) -> str:
        """Return a divider sized to the current terminal without becoming noisy."""
        width = min(max(shutil.get_terminal_size(fallback=(80, 24)).columns, 48), 88)
        return "-" * width

    def show_header(self, model: str, host: str, workspace: Path) -> None:
        """Display the application identity and active session details."""
        print(self.style(self.rule(), "2"))
        print(self.style("  CODING AGENT", "1;36"))
        print(self.style(self.rule(), "2"))
        print(f"  Model      {model}")
        print(f"  Server     {host}")
        print(f"  Workspace  {workspace}")
        print(self.style("  Commands   clear  reset conversation    quit  exit", "2"))
        print(self.style(self.rule(), "2"))
        print()

    def prompt(self) -> str:
        """Read one user message with a distinct conversation label."""
        return input(self.style("You  > ", "1;32")).strip()

    def begin_response(self) -> None:
        """Open an assistant response section if one is not already active."""
        if not self.response_open:
            print(self.style("Agent> ", "1;36"), end="", flush=True)
            self.response_open = True

    def write_response(self, text: str) -> None:
        """Display a streamed fragment without allowing carriage-return overwrites."""
        if not text:
            return
        self.begin_response()
        safe_text = text.replace("\r\n", "\n").replace("\r", "\n")
        print(safe_text, end="", flush=True)

    def finish_response(self) -> None:
        """Close the current response and leave space before the next prompt."""
        if self.response_open:
            print()
        print()
        self.response_open = False

    def show_tool_call(self, activity: str) -> None:
        """Show bounded, human-readable model tool activity on its own line."""
        if self.response_open:
            print()
        print(self.style(f"  Working    {_terminal_text(activity)}", "2;36"), flush=True)
        self.response_open = False

    def show_permission_receipt(self, message: str) -> None:
        """Show a quiet permission outcome alongside tool activity."""
        if self.response_open:
            print()
        print(self.style(f"  Permission {message}", "2;36"), flush=True)
        self.response_open = False

    def show_notice(self, label: str, message: str, style_code: str = "33") -> None:
        """Render a labeled status, error, or recovery notice."""
        if self.response_open:
            print()
        print(f"{self.style(f'{label:<11}', style_code)}{message}\n")
        self.response_open = False

    def _read_key(self) -> str:
        """Read one navigation key without requiring Enter."""
        if os.name == "nt":
            import msvcrt

            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "other")
        else:
            import termios
            import tty

            if not sys.stdin.isatty():
                key = sys.stdin.read(1)
                if key == "\x1b":
                    sequence = sys.stdin.read(2)
                    return {"[A": "up", "[B": "down"}.get(sequence, "other")
                if key == "\x03":
                    raise KeyboardInterrupt
                if key == "":
                    raise EOFError
                return "confirm" if key in {" ", "\r", "\n"} else "other"
            descriptor = sys.stdin.fileno()
            previous = termios.tcgetattr(descriptor)
            try:
                tty.setraw(descriptor)
                key = sys.stdin.read(1)
                if key == "\x1b":
                    sequence = sys.stdin.read(2)
                    return {"[A": "up", "[B": "down"}.get(sequence, "other")
            finally:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        if key == "\x03":
            raise KeyboardInterrupt
        if key == "":
            raise EOFError
        return "confirm" if key in {" ", "\r", "\n"} else "other"

    def choose_option(
        self,
        title: str,
        lines: list[str],
        options: list[str],
        *,
        initial: int = 0,
        transient: bool = True,
    ) -> int:
        """Select one dialogue option with arrow keys and confirm with Space or Enter."""
        use_alternate_screen = transient and sys.stdin.isatty() and sys.stdout.isatty()
        if use_alternate_screen:
            print("\033[?1049h", end="", flush=True)
        selected = initial
        try:
            while True:
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="", flush=True)
                print(self.style(self.rule(), "2"))
                print(self.style(f"  {title.upper()}", "1;36"))
                print(self.style("  Use Up/Down to select, then Space or Enter to confirm", "2;36"))
                print(self.style(self.rule(), "2"))
                for line in lines:
                    print(f"  {line}")
                if lines:
                    print()
                for index, option in enumerate(options):
                    marker = ">" if index == selected else " "
                    style_code = "1;32" if index == selected else "2;36"
                    print(self.style(f"  {marker} {option}", style_code))
                print(self.style(self.rule(), "2"))
                key = self._read_key()
                if key == "up":
                    selected = (selected - 1) % len(options)
                elif key == "down":
                    selected = (selected + 1) % len(options)
                elif key == "confirm":
                    return selected
        finally:
            if use_alternate_screen:
                print("\033[?1049l", end="", flush=True)

    def request_directory_access(self, directory: Path) -> AccessChoice:
        """Ask for session-scoped directory access in a transient screen."""
        options = [
            "Allow this directory for this session",
            "Allow all directories for this session",
            "Deny this directory for this session",
        ]
        try:
            selected = self.choose_option(
                "Directory permission",
                [
                    "The agent needs access outside the workspace.",
                    "",
                    _terminal_text(directory),
                ],
                options,
                initial=0,
            )
            decision = AccessChoice(selected + 1)
        except (KeyboardInterrupt, EOFError):
            decision = AccessChoice.DENY
        if decision == AccessChoice.GRANT_DIRECTORY:
            self.show_permission_receipt(
                f"Allowed access to {_terminal_text(directory)} for this session."
            )
        elif decision == AccessChoice.GRANT_ALL:
            self.show_permission_receipt("Allowed access to all directories for this session.")
        else:
            self.show_permission_receipt(
                f"Denied access to {_terminal_text(directory)} for this session."
            )
        return decision

    def request_tool_approval(self, request: ApprovalRequest) -> ApprovalPromptResult:
        """Ask whether one summarized ask-first operation may execute."""
        edit_targets = _file_edit_targets(request)
        command_executable = _command_executable(request)
        if edit_targets and set(edit_targets).issubset(self.approved_edit_files):
            self.show_permission_receipt(_tool_approval_receipt(request, True, reused=True))
            return ApprovalPromptResult(True, "session_file_grant")
        if command_executable in self.approved_command_executables:
            self.show_permission_receipt(_tool_approval_receipt(request, True, reused=True))
            return ApprovalPromptResult(True, "session_command_grant")

        options = [
            "Allow this command once"
            if command_executable is not None
            else "Allow once"
        ]
        if edit_targets:
            noun = "this file" if len(edit_targets) == 1 else "these files"
            options.append(f"Allow all edits to {noun} for this session")
        if command_executable is not None:
            options.append(
                f"Allow all {_terminal_text(command_executable, 80)} commands for this session"
            )
        options.append("Deny this command" if command_executable is not None else "Deny")
        try:
            selected = self.choose_option(
                "Tool permission",
                _tool_approval_summary(request),
                options,
                initial=0,
            )
        except (KeyboardInterrupt, EOFError):
            selected = len(options) - 1
        allowed = selected < len(options) - 1
        file_session_grant = bool(edit_targets) and selected == 1
        command_session_grant = command_executable is not None and selected == 1
        if file_session_grant:
            self.approved_edit_files.update(edit_targets)
        if command_session_grant:
            self.approved_command_executables.add(command_executable)
        self.show_permission_receipt(
            _tool_approval_receipt(
                request,
                allowed,
                session_grant=file_session_grant or command_session_grant,
            )
        )
        source = (
            "user_session_grant"
            if file_session_grant or command_session_grant
            else "user"
        )
        return ApprovalPromptResult(allowed, source)

    def show_goodbye(self) -> None:
        """End the session with a quiet, consistent message."""
        if self.response_open:
            print()
        print("Session ended.")
        self.response_open = False


def select_model(
    models: tuple[str, ...],
    default: str,
    ui: TerminalUI | None = None,
) -> str:
    """Ask which configured model to use for this session."""
    if len(models) == 1:
        return models[0]

    active_ui = ui or TerminalUI()
    options = []
    for model in models:
        description = MODEL_DESCRIPTIONS.get(model)
        label = model if description is None else f"{model} - {description}"
        if model == default:
            label += " (default)"
        options.append(label)
    selected = active_ui.choose_option(
        "Coding Agent - Select a model",
        [],
        options,
        initial=models.index(default),
        transient=False,
    )
    return models[selected]


def _tool_approval_summary(request: ApprovalRequest) -> list[str]:
    """Describe a tool operation without exposing large or sensitive payloads."""
    lines = [
        f"Risk       {_terminal_text(request.category.value)}",
        f"Operation  {_terminal_text(request.tool_name)}",
    ]
    if request.tool_name == "run_tests" and isinstance(request.arguments, dict):
        targets = request.arguments.get("targets")
        if targets is None:
            lines.append("Scope      Full configured test suite")
        elif isinstance(targets, list):
            lines.append(f"Scope      {len(targets)} targeted test selector(s)")
            for target in targets[:10]:
                lines.append(f"- {_terminal_text(target)}")
            if len(targets) > 10:
                lines.append(f"- and {len(targets) - 10} more targets")
        lines.append(f"Timeout    {TEST_TIMEOUT_SECONDS} seconds")
        return lines
    if request.tool_name == "run_checks" and isinstance(request.arguments, dict):
        name = request.arguments.get("name", "unknown check")
        target = request.arguments.get("target")
        lines.append(f"Check      {_terminal_text(name)}")
        if isinstance(target, str):
            lines.append(f"Target     {_terminal_text(target)}")
        lines.append(f"Timeout    {TEST_TIMEOUT_SECONDS} seconds")
        return lines
    if request.tool_name == "run_command" and isinstance(request.arguments, dict):
        command = request.arguments.get("command")
        if isinstance(command, list) and command:
            lines.append(f"Executable  {_terminal_text(command[0])}")
            lines.append(f"Arguments   {max(0, len(command) - 1)}")
        lines.append(f"Directory   {_terminal_text(Path.cwd())}")
        lines.append(f"Timeout     {TEST_TIMEOUT_SECONDS} seconds")
        return lines
    if request.tool_name == "install_dependencies" and isinstance(request.arguments, dict):
        lines.append(f"Project     {_terminal_text(request.arguments.get('path', '.'))}")
        lines.append(f"Directory   {_terminal_text(Path.cwd())}")
        lines.append(f"Timeout     {TEST_TIMEOUT_SECONDS} seconds")
        return lines
    if request.tool_name != "apply_file_changes" or not isinstance(request.arguments, dict):
        return lines

    changes = request.arguments.get("changes")
    if not isinstance(changes, list):
        return lines
    lines.append(f"Files      {len(changes)}")
    base_path = request.arguments.get("base_path")
    if isinstance(base_path, str):
        lines.append(f"Base path  {_terminal_text(base_path)}")
    for change in changes[:10]:
        if not isinstance(change, dict):
            continue
        action = change.get("action", "change")
        path = change.get("path", "unknown path")
        lines.append(f"- {_terminal_text(action, 24)}: {_terminal_text(path)}")
    if len(changes) > 10:
        lines.append(f"- and {len(changes) - 10} more files")
    return lines


def _file_edit_targets(request: ApprovalRequest) -> list[Path] | None:
    """Return exact file targets eligible for a session edit grant."""
    if request.tool_name != "apply_file_changes":
        return None
    targets = file_change_target_paths(request.arguments)
    return targets if isinstance(targets, list) else None


def _command_executable(request: ApprovalRequest) -> str | None:
    """Return the exact normalized executable eligible for a session grant."""
    if request.tool_name != "run_command" or not isinstance(request.arguments, dict):
        return None
    command = request.arguments.get("command")
    if not isinstance(command, list) or not command or not isinstance(command[0], str):
        return None
    executable = command[0]
    if not executable or any(not character.isprintable() for character in executable):
        return None
    return executable.casefold()


def _tool_approval_receipt(
    request: ApprovalRequest,
    allowed: bool,
    *,
    session_grant: bool = False,
    reused: bool = False,
) -> str:
    """Return a compact conversation receipt for a tool permission decision."""
    decision = "Allowed" if allowed else "Denied"
    if request.tool_name == "run_tests" and isinstance(request.arguments, dict):
        targets = request.arguments.get("targets")
        if targets is None:
            scope = "the full test suite"
        elif isinstance(targets, list):
            scope = f"{len(targets)} test target(s)"
        else:
            scope = "the requested test operation"
        return f"{decision} {scope}."
    if request.tool_name == "run_checks" and isinstance(request.arguments, dict):
        name = _terminal_text(request.arguments.get("name", "requested check"))
        target = request.arguments.get("target")
        scope = f"check {name}"
        if isinstance(target, str):
            scope += f" target {_terminal_text(target)}"
        return f"{decision} {scope}."
    if request.tool_name == "run_command" and isinstance(request.arguments, dict):
        command = request.arguments.get("command")
        executable = command[0] if isinstance(command, list) and command else "command"
        if reused:
            return f"Used existing command access for {_terminal_text(executable)}."
        if session_grant:
            return (
                f"Allowed all {_terminal_text(executable)} commands for this session."
            )
        return f"{decision} command {_terminal_text(executable)} in the active workspace."
    if request.tool_name == "install_dependencies" and isinstance(request.arguments, dict):
        path = _terminal_text(request.arguments.get("path", "."))
        return f"{decision} dependency installation for {path}."
    if request.tool_name == "apply_file_changes" and isinstance(request.arguments, dict):
        changes = request.arguments.get("changes")
        if isinstance(changes, list):
            noun = "file change" if len(changes) == 1 else "file changes"
            if reused:
                return f"Used existing edit access for {len(changes)} {noun}."
            if session_grant:
                file_noun = "file" if len(changes) == 1 else "files"
                return f"Allowed all edits to {len(changes)} {file_noun} for this session."
            return f"{decision} {len(changes)} {noun}."
    return f"{decision} {_terminal_text(request.tool_name)}."


def request_directory_access(directory: Path) -> AccessChoice:
    """Ask for one session-scoped filesystem permission decision."""
    return TerminalUI().request_directory_access(directory)


def request_tool_approval(request: ApprovalRequest) -> ApprovalPromptResult:
    """Ask whether one exact ask-first tool operation may execute."""
    return TerminalUI().request_tool_approval(request)


def recover_turn(messages: list, turn_start: int) -> None:
    """Keep recorded tool outcomes; discard failed turns that performed no tools."""
    if any(message["role"] == "tool" for message in messages[turn_start:]):
        messages.append({
            "role": "assistant",
            "content": "Turn interrupted. Recorded tool outcomes are retained; "
            "inspect any unknown outcome before retrying an action.",
        })
    else:
        del messages[turn_start:]


def main() -> None:
    """Read prompts and keep conversation history until the user exits."""
    ui = TerminalUI()
    ui.clear_screen()
    try:
        selected_model = select_model(MODELS, MODEL, ui)
    except (KeyboardInterrupt, EOFError):
        ui.show_goodbye()
        return
    ui.clear_screen()
    client = Client(host=HOST)
    access = WorkspaceAccess(Path.cwd())
    messages = new_conversation()
    interaction_logger = InteractionLogger(LOG_DIRECTORY, LOG_REDACT_VALUES)
    ui.show_header(selected_model, HOST, Path.cwd())

    while True:
        try:
            user_input = ui.prompt()
        except (KeyboardInterrupt, EOFError):
            print()
            ui.show_goodbye()
            break
        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            ui.show_goodbye()
            break
        if user_input.lower() == "clear":
            messages = new_conversation()
            ui.show_notice("Reset", "Conversation cleared.", "32")
            continue

        turn_start = len(messages)
        messages.append({"role": "user", "content": user_input})
        turn_log = interaction_logger.start_turn(user_input)
        outcome = "failed"
        error_detail = None
        try:
            outcome = run_turn(
                client=client,
                model=selected_model,
                messages=messages,
                on_text=ui.write_response,
                on_tool=ui.show_tool_call,
                access=access,
                on_permission=ui.request_directory_access,
                turn_log=turn_log,
                on_approval=ui.request_tool_approval,
            ) or "success"
            ui.finish_response()
        except ContextBudgetError as error:
            outcome = "context_limit"
            error_detail = str(error)
            ui.show_notice("Context", str(error))
        except EmptyModelResponseError as error:
            outcome = "model_error"
            error_detail = str(error)
            recover_turn(messages, turn_start)
            ui.show_notice("Model error", f"Model response failed: {error}", "31")
        except ResponseError as error:
            outcome = "model_error"
            error_detail = f"{error.status_code} - {error.error}"
            recover_turn(messages, turn_start)
            ui.show_notice("Ollama", f"{error.status_code} - {error.error}", "31")
        except KeyboardInterrupt:
            outcome = "cancelled"
            error_detail = "Request cancelled."
            recover_turn(messages, turn_start)
            ui.show_notice("Cancelled", "Request cancelled.")
        except Exception as error:
            error_detail = str(error)
            recover_turn(messages, turn_start)
            ui.show_notice("Error", f"Request failed: {error}", "31")
        finally:
            interaction_logger.finish_turn(turn_log, outcome, error_detail)
