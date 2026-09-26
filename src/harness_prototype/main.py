"""Interactive terminal entry point."""

import os
from pathlib import Path
import shutil
import sys

from ollama import Client, ResponseError

from .access import AccessChoice, WorkspaceAccess
from .agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    new_conversation,
    run_turn,
)
from .config import HOST, LOG_DIRECTORY, LOG_REDACT_VALUES, MODEL, MODELS
from .interaction_log import InteractionLogger


MODEL_DESCRIPTIONS = {
    "devstral-small-2:latest": "more capable; slower when it does not fit fully in GPU memory",
    "qwen3:8b": "faster and lighter; may be less capable on complex tasks",
}


class TerminalUI:
    """Render the conversation with a compact, consistent terminal layout."""

    def __init__(self) -> None:
        """Enable ANSI styling only for an interactive terminal that permits it."""
        self.use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.response_open = False

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

    def show_model_selection(
        self,
        models: tuple[str, ...],
        default: str,
        descriptions: dict[str, str],
    ) -> None:
        """Display configured models as the temporary startup screen."""
        print(self.style(self.rule(), "2"))
        print(self.style("  CODING AGENT", "1;36"))
        print(self.style("  Select a model for this session", "2"))
        print(self.style(self.rule(), "2"))
        for number, model in enumerate(models, start=1):
            marker = "default" if model == default else ""
            print(f"  {self.style(str(number), '1;36')}  {model:<30} {marker}")
            description = descriptions.get(model)
            if description:
                print(self.style(f"     {description}", "2"))
        print(self.style(self.rule(), "2"))

    def model_prompt(self, model_count: int, default_number: int) -> str:
        """Read a model number from the startup screen."""
        prompt = f"Select [1-{model_count}]  (default {default_number}) > "
        return input(self.style(prompt, "1;32")).strip()

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

    def show_tool_call(self, name: str) -> None:
        """Show model tool activity on its own line."""
        if self.response_open:
            print()
        print(self.style(f"  Working    {name}", "2;36"), flush=True)
        self.response_open = False

    def show_notice(self, label: str, message: str, style_code: str = "33") -> None:
        """Render a labeled status, error, or recovery notice."""
        if self.response_open:
            print()
        print(f"{self.style(f'{label:<11}', style_code)}{message}\n")
        self.response_open = False

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
    default_number = models.index(default) + 1
    active_ui.show_model_selection(models, default, MODEL_DESCRIPTIONS)

    while True:
        choice = active_ui.model_prompt(len(models), default_number)
        if not choice:
            return default
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        print(f"Enter a number from 1 to {len(models)}.")


def request_directory_access(directory: Path) -> AccessChoice:
    """Ask for one session-scoped filesystem permission decision."""
    print("\nPermission required")
    print("The agent requested a directory outside the workspace:")
    print(f"  {directory}\n")
    print(f"  1  Grant access to {directory} for this session")
    print("  2  Grant full machine access for this session")
    print("  3  Deny access and remember this decision")
    while True:
        try:
            choice = input("Choose 1, 2, or 3: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAccess denied.")
            return AccessChoice.DENY
        if choice in {"1", "2", "3"}:
            return AccessChoice(int(choice))
        print("Please enter 1, 2, or 3.")


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
                on_permission=request_directory_access,
                turn_log=turn_log,
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
