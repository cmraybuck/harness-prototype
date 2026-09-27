"""Interactive terminal entry point."""

import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ollama import Client

from .access import AccessChoice
from .approval import ApprovalPromptResult, ApprovalRequest
from .agent_loop import (
    AgentRuntime,
    compose_request,
    estimate_context_usage,
    run_turn,
)
from .config import (
    CONTEXT_LENGTH,
    HOST,
    LOG_DIRECTORY,
    LOG_REDACT_VALUES,
    MODELS,
    TEST_TIMEOUT_SECONDS,
)
from .documentation import DOCUMENTATION_TIMEOUT_SECONDS
from .files import file_change_target_paths
from .interaction_log import InteractionLogger
from .services import stop_all_services
from .tools import TOOL_SPECS


MODEL_DESCRIPTIONS = {
    "devstral-small-2:latest": "strong coding and agentic tool use; heavier and slower",
    "qwen3:8b": "fast, lightweight text reasoning and tool use",
    "qwen3-vl:8b": "vision-language reasoning for images, text, coding, and tool use",
}

APP_DISPLAY_NAME = "FLUX"
APP_DESCRIPTION = "A local-first software engineering agent powered by Ollama."

COMMAND_DESCRIPTIONS = {
    "/clear": "Start a fresh conversation with the current model",
    "/model": "Switch models and start a fresh conversation",
    "/tools": "Show the tools available to the agent",
    "/skills": "List discovered local skills",
    "/skill": "Activate skills: /skill id[,id] (empty clears)",
    "/tool-activity": "Choose how tool activity appears",
    "/status": "Show the current session settings",
    "/help": "Show slash commands and keyboard controls",
    "/exit": "End the session",
}

TOOL_ACTIVITY_MODES = ("full", "compact", "hidden")


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
        self.documentation_access_approved = False
        self.tool_activity_mode = "full"
        self._prompt_render_rows = 0
        self._prompt_cursor_row = 0

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
        print(self.style(f"  {APP_DISPLAY_NAME}", "1;36"))
        print(self.style(self.rule(), "2"))
        print(f"  Model      {model}")
        print(f"  Server     {host}")
        print(f"  Workspace  {workspace}")
        print(self.style("  Commands   /  browse commands    /help  show help", "2"))
        print(self.style(self.rule(), "2"))
        print()

    def prompt(self) -> str:
        """Read one user message with inline slash-command suggestions."""
        prompt_text = self.style("You  > ", "1;32")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return input(prompt_text).strip()

        buffer = ""
        cursor = 0
        selected = 0
        suggestions_suppressed = False
        while True:
            matches = [] if suggestions_suppressed else self._command_matches(buffer)
            if matches:
                selected = min(selected, len(matches) - 1)
            else:
                selected = 0
            self._render_prompt_input(prompt_text, buffer, cursor, matches, selected)
            key = self._read_key()
            if key == "up" and matches:
                selected = (selected - 1) % len(matches)
            elif key == "down" and matches:
                selected = (selected + 1) % len(matches)
            elif key == "left":
                cursor = max(0, cursor - 1)
            elif key == "right":
                cursor = min(len(buffer), cursor + 1)
            elif key == "confirm":
                submitted = matches[selected] if matches else buffer
                self._render_prompt_input(
                    prompt_text, submitted, len(submitted), [], 0, finish=True
                )
                return submitted.strip()
            elif key == "backspace":
                if cursor:
                    buffer = buffer[: cursor - 1] + buffer[cursor:]
                    cursor -= 1
                selected = 0
                suggestions_suppressed = False
            elif key == "cancel" and matches:
                suggestions_suppressed = True
            elif key == "space":
                buffer = buffer[:cursor] + " " + buffer[cursor:]
                cursor += 1
                selected = 0
                suggestions_suppressed = False
            elif len(key) == 1 and key.isprintable():
                buffer = buffer[:cursor] + key + buffer[cursor:]
                cursor += 1
                selected = 0
                suggestions_suppressed = False

    def _command_matches(self, buffer: str) -> list[str]:
        """Return command-name prefix matches for the current prompt text."""
        if not buffer.startswith("/") or any(character.isspace() for character in buffer):
            return []
        normalized = buffer.casefold()
        return [command for command in COMMAND_DESCRIPTIONS if command.startswith(normalized)]

    def _render_prompt_input(
        self,
        prompt_text: str,
        buffer: str,
        cursor: int,
        matches: list[str],
        selected: int,
        *,
        finish: bool = False,
    ) -> None:
        """Redraw editable input and command suggestions beneath it."""
        if self._prompt_render_rows:
            if self._prompt_cursor_row:
                print(f"\033[{self._prompt_cursor_row}A", end="")
            print("\r\033[J", end="")
        width = max(12, shutil.get_terminal_size(fallback=(80, 24)).columns)
        input_lines, cursor_row, cursor_column = self._prompt_input_layout(
            prompt_text, buffer, cursor, width,
        )
        rendered_lines = list(input_lines)
        for index, command in enumerate(matches):
            marker = ">" if index == selected else " "
            style_code = "1;32" if index == selected else "2;36"
            description = COMMAND_DESCRIPTIONS[command]
            suggestion = _terminal_text(
                f"  {marker} {command:<16} {description}", width - 1,
            )
            rendered_lines.append(self.style(suggestion, style_code))
        print("\n".join(rendered_lines), end="")
        current_row = len(rendered_lines) - 1
        rows_up = current_row - cursor_row
        if rows_up:
            print(f"\033[{rows_up}A", end="")
        print("\r", end="")
        if cursor_column:
            print(f"\033[{cursor_column}C", end="")
        self._prompt_render_rows = len(rendered_lines)
        self._prompt_cursor_row = cursor_row
        if finish:
            print("\033[J", flush=True)
            self._prompt_render_rows = 0
            self._prompt_cursor_row = 0
            return
        print(end="", flush=True)

    @staticmethod
    def _prompt_input_layout(
        prompt_text: str,
        buffer: str,
        cursor: int,
        terminal_width: int,
    ) -> tuple[list[str], int, int]:
        """Wrap editable input and locate its cursor without terminal auto-wrap."""
        prefix_width = len("You  > ")
        content_width = max(1, terminal_width - prefix_width - 1)
        chunks = [
            buffer[index:index + content_width]
            for index in range(0, len(buffer), content_width)
        ]
        if not chunks or len(buffer) % content_width == 0:
            chunks.append("")
        continuation = " " * prefix_width
        lines = [prompt_text + chunks[0]]
        lines.extend(continuation + chunk for chunk in chunks[1:])
        cursor_row, cursor_offset = divmod(cursor, content_width)
        return lines, cursor_row, prefix_width + cursor_offset

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
        if self.tool_activity_mode == "hidden":
            if self.response_open:
                print()
                self.response_open = False
            return
        if self.response_open:
            print()
        max_chars = 80 if self.tool_activity_mode == "compact" else 160
        print(
            self.style(f"  Working    {_terminal_text(activity, max_chars)}", "2;36"),
            flush=True,
        )
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

    def show_command_help(self) -> None:
        """Display slash commands and their keyboard controls."""
        lines = [
            f"{command:<16} {description}"
            for command, description in COMMAND_DESCRIPTIONS.items()
        ]
        lines.append("")
        lines.append("Start with / to see suggestions; keep typing to filter by command name.")
        lines.append("Use Up/Down and Enter to choose; Esc hides suggestions.")
        self.show_notice("Commands", "\n".join(lines), "36")

    def show_tools(self) -> None:
        """Display registered model tools with concise descriptions."""
        lines = []
        for name, spec in TOOL_SPECS.items():
            docstring = spec.function.__doc__ or "No description available."
            description = " ".join(docstring.strip().split()).split(". ", 1)[0]
            lines.append(f"{name:<30} {description}")
        self.show_notice("Tools", "\n".join(lines), "36")

    def choose_tool_activity_mode(self) -> str | None:
        """Ask how routine tool calls should appear in the conversation."""
        descriptions = {
            "full": "Show complete one-line activity descriptions",
            "compact": "Show shorter activity descriptions",
            "hidden": "Hide routine activity; permissions and errors remain visible",
        }
        options = [f"{mode:<8} {descriptions[mode]}" for mode in TOOL_ACTIVITY_MODES]
        try:
            initial = TOOL_ACTIVITY_MODES.index(self.tool_activity_mode)
            selected = self.choose_option(
                "Tool activity",
                ["This changes display only; tools remain available to the agent."],
                options,
                initial=initial,
            )
        except (KeyboardInterrupt, EOFError):
            return None
        return TOOL_ACTIVITY_MODES[selected]

    def _read_key(self) -> str:
        """Read one navigation key without requiring Enter."""
        if os.name == "nt":
            import msvcrt

            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                return {
                    "H": "up", "P": "down", "K": "left", "M": "right",
                }.get(msvcrt.getwch(), "other")
        else:
            import select
            import termios
            import tty

            if not sys.stdin.isatty():
                key = sys.stdin.read(1)
                if key == "\x1b":
                    sequence = sys.stdin.read(2)
                    return {
                        "[A": "up", "[B": "down", "[C": "right", "[D": "left",
                    }.get(sequence, "other")
                if key == "\x03":
                    raise KeyboardInterrupt
                if key == "":
                    raise EOFError
                if key in {"\b", "\x7f"}:
                    return "backspace"
                if key == "\x1b":
                    return "cancel"
                if key == " ":
                    return "space"
                return "confirm" if key in {"\r", "\n"} else key
            descriptor = sys.stdin.fileno()
            previous = termios.tcgetattr(descriptor)
            try:
                tty.setraw(descriptor)
                key = sys.stdin.read(1)
                if key == "\x1b":
                    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if not ready:
                        return "cancel"
                    sequence = sys.stdin.read(2)
                    return {
                        "[A": "up", "[B": "down", "[C": "right", "[D": "left",
                    }.get(sequence, "other")
            finally:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        if key == "\x03":
            raise KeyboardInterrupt
        if key == "":
            raise EOFError
        if key in {"\b", "\x7f"}:
            return "backspace"
        if key == "\x1b":
            return "cancel"
        if key == " ":
            return "space"
        return "confirm" if key in {"\r", "\n"} else key

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
                print(self.style(
                    "  Use Up/Down to select, then Space or Enter to confirm", "2;36"
                ))
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
                elif key in {"confirm", "space"}:
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
        documentation_request = request.tool_name == "fetch_documentation"
        if edit_targets and set(edit_targets).issubset(self.approved_edit_files):
            self.show_permission_receipt(_tool_approval_receipt(request, True, reused=True))
            return ApprovalPromptResult(True, "session_file_grant")
        if command_executable in self.approved_command_executables:
            self.show_permission_receipt(_tool_approval_receipt(request, True, reused=True))
            return ApprovalPromptResult(True, "session_command_grant")
        if documentation_request and self.documentation_access_approved:
            self.show_permission_receipt(_tool_approval_receipt(request, True, reused=True))
            return ApprovalPromptResult(True, "session_documentation_grant")

        options = [
            "Allow this command once"
            if command_executable is not None
            else (
                "Allow this documentation request"
                if documentation_request
                else "Allow once"
            )
        ]
        if edit_targets:
            noun = "this file" if len(edit_targets) == 1 else "these files"
            options.append(f"Allow all edits to {noun} for this session")
        if command_executable is not None:
            options.append(
                f"Allow all {_terminal_text(command_executable, 80)} commands for this session"
            )
        if documentation_request:
            options.append("Allow documentation access for this session")
        options.append(
            "Deny this command"
            if command_executable is not None
            else (
                "Deny this documentation request"
                if documentation_request
                else "Deny"
            )
        )
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
        documentation_session_grant = documentation_request and selected == 1
        if file_session_grant:
            self.approved_edit_files.update(edit_targets)
        if command_session_grant:
            self.approved_command_executables.add(command_executable)
        if documentation_session_grant:
            self.documentation_access_approved = True
        self.show_permission_receipt(
            _tool_approval_receipt(
                request,
                allowed,
                session_grant=(
                    file_session_grant
                    or command_session_grant
                    or documentation_session_grant
                ),
            )
        )
        source = (
            "user_session_grant"
            if file_session_grant or command_session_grant or documentation_session_grant
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
        options.append(label)
    selected = active_ui.choose_option(
        f"{APP_DISPLAY_NAME} - Select a model",
        [],
        options,
        initial=0,
        transient=False,
    )
    return models[selected]


def confirm_model_switch(model: str, ui: TerminalUI) -> bool:
    """Confirm that changing models resets retained conversation context."""
    options = [
        "Switch model and start a new conversation",
        "Keep the current model and conversation",
    ]
    try:
        selected = ui.choose_option(
            "Start a new conversation?",
            [
                f"Selected model: {_terminal_text(model)}",
                "Switching models clears the current conversation context.",
            ],
            options,
            initial=1,
        )
    except (KeyboardInterrupt, EOFError):
        return False
    return selected == 0


def _tool_approval_summary(request: ApprovalRequest) -> list[str]:
    """Describe a tool operation without exposing large or sensitive payloads."""
    lines = [
        f"Risk       {_terminal_text(request.category.value)}",
        f"Operation  {_terminal_text(request.tool_name)}",
    ]
    if request.tool_name == "fetch_documentation" and isinstance(request.arguments, dict):
        raw_url = request.arguments.get("url")
        hostname = urlsplit(raw_url).hostname if isinstance(raw_url, str) else None
        lines.append(f"Host       {_terminal_text(hostname or 'invalid URL')}")
        lines.append(f"Timeout    {DOCUMENTATION_TIMEOUT_SECONDS} seconds")
        return lines
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
    if request.tool_name == "start_service" and isinstance(request.arguments, dict):
        lines.append(f"Service     {_terminal_text(request.arguments.get('name', 'unknown'))}")
        lines.append(f"Directory   {_terminal_text(Path.cwd())}")
        return lines
    if request.tool_name == "stop_service" and isinstance(request.arguments, dict):
        lines.append(f"Scope       One {APP_DISPLAY_NAME}-managed service handle")
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
    if request.tool_name == "fetch_documentation" and isinstance(request.arguments, dict):
        raw_url = request.arguments.get("url")
        hostname = urlsplit(raw_url).hostname if isinstance(raw_url, str) else None
        if reused:
            return "Used existing documentation access for this session."
        if session_grant:
            return "Allowed documentation access for this session."
        return f"{decision} one documentation request to {_terminal_text(hostname or 'invalid host')}."
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
    if request.tool_name == "start_service" and isinstance(request.arguments, dict):
        name = _terminal_text(request.arguments.get("name", "unknown"))
        return f"{decision} service start for {name}."
    if request.tool_name == "stop_service":
        return f"{decision} stopping one {APP_DISPLAY_NAME}-managed service."
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


def main() -> None:
    """Read prompts and keep conversation history until the user exits."""
    ui = TerminalUI()
    ui.clear_screen()
    try:
        selected_model = select_model(MODELS, ui)
    except (KeyboardInterrupt, EOFError):
        ui.show_goodbye()
        return
    ui.clear_screen()
    client = Client(host=HOST)
    interaction_logger = InteractionLogger(LOG_DIRECTORY, LOG_REDACT_VALUES)
    runtime = AgentRuntime(
        client,
        selected_model,
        Path.cwd(),
        interaction_logger=interaction_logger,
        turn_runner=run_turn,
    )
    ui.show_header(selected_model, HOST, Path.cwd())

    try:
        while True:
            try:
                user_input = ui.prompt()
            except (KeyboardInterrupt, EOFError):
                print()
                ui.show_goodbye()
                break
            if not user_input:
                continue
            normalized_input = user_input.casefold()
            if normalized_input in {"quit", "exit", "/exit"}:
                ui.show_goodbye()
                break
            if normalized_input in {"clear", "/clear"}:
                runtime.clear_conversation()
                ui.show_notice("Reset", "Conversation cleared.", "32")
                continue
            if normalized_input == "/model":
                try:
                    requested_model = select_model(MODELS, ui)
                except (KeyboardInterrupt, EOFError):
                    ui.show_notice("Model", "Model selection cancelled.")
                    continue
                if requested_model == selected_model:
                    ui.show_notice("Model", f"{selected_model} is already active.", "36")
                    continue
                if not confirm_model_switch(requested_model, ui):
                    ui.show_notice("Model", "Model switch cancelled.")
                    continue
                selected_model = requested_model
                runtime.change_model(selected_model)
                ui.show_notice(
                    "Model",
                    f"Switched to {selected_model}. Started a new conversation.",
                    "32",
                )
                continue
            if normalized_input == "/tools":
                ui.show_tools()
                continue
            if normalized_input == "/skills":
                catalog = runtime.list_skills()
                lines = [
                    f"{item['id']:<24} {item['name']} — {item['description']}"
                    for item in catalog["skills"]
                ]
                if not lines:
                    lines.append("No valid local skills discovered.")
                lines.extend(
                    f"Invalid {item['path']}: {item['error']}"
                    for item in catalog["diagnostics"]
                )
                lines.append("Roots: " + ", ".join(catalog["roots"]))
                ui.show_notice("Skills", "\n".join(lines), "36")
                continue
            if normalized_input == "/skill" or normalized_input.startswith("/skill "):
                argument = user_input[6:].strip()
                identifiers = [item.strip() for item in argument.split(",") if item.strip()]
                activation = runtime.activate_skills(identifiers)
                if activation["ok"]:
                    active = activation["active_skills"]
                    message = "Active skills: " + (", ".join(active) if active else "none")
                    ui.show_notice("Skills", message, "32")
                else:
                    ui.show_notice("Skills", activation["error"], "31")
                continue
            if normalized_input == "/tool-activity":
                mode = ui.choose_tool_activity_mode()
                if mode is None:
                    ui.show_notice("Tools", "Tool activity display unchanged.")
                    continue
                ui.tool_activity_mode = mode
                ui.show_notice("Tools", f"Tool activity display set to {mode}.", "32")
                continue
            if normalized_input == "/status":
                composition = compose_request(
                    runtime.messages, runtime.capability_state, runtime.skill_state
                )
                context_used = estimate_context_usage(
                    runtime.messages,
                    runtime.capability_state,
                    runtime.skill_state,
                )
                context_percent = context_used / CONTEXT_LENGTH
                ui.show_notice(
                    "Status",
                    "\n" + "\n".join([
                        f"  Model          {selected_model}",
                        f"  Server         {HOST}",
                        f"  Workspace      {Path.cwd()}",
                        f"  Messages       {max(0, len(runtime.messages) - 1)} retained",
                        f"  Context used   ~{context_used} tokens ({context_percent:.0%})",
                        f"  Context limit  {CONTEXT_LENGTH} tokens (heuristic guard)",
                        "  Capabilities   " + (
                            ", ".join(composition.active_groups) or "none active"
                        ),
                        "  Skills         " + (
                            ", ".join(composition.active_skills) or "none active"
                        ),
                        f"  Model tools    {', '.join(composition.tool_names)}",
                        "  Context chars  " + ", ".join(
                            f"{name}={count}"
                            for name, count in composition.character_counts.items()
                        ),
                        f"  Tool activity  {ui.tool_activity_mode}",
                    ]),
                    "36",
                )
                continue
            if normalized_input == "/help":
                ui.show_command_help()
                continue
            result = runtime.continue_conversation(
                user_input,
                on_text=ui.write_response,
                on_tool=ui.show_tool_call,
                on_permission=ui.request_directory_access,
                on_approval=ui.request_tool_approval,
            )
            if result.outcome not in {"context_limit", "model_error", "cancelled", "failed"}:
                ui.finish_response()
            elif result.outcome == "context_limit":
                ui.show_notice("Context", result.error or "Context limit reached.")
            elif result.outcome == "model_error":
                ui.show_notice("Model error", result.error or "Model request failed.", "31")
            elif result.outcome == "cancelled":
                ui.show_notice("Cancelled", result.error or "Request cancelled.")
            else:
                ui.show_notice("Error", result.error or "Request failed.", "31")
    finally:
        stop_all_services()
