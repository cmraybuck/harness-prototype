"""Interactive terminal entry point."""

from pathlib import Path

from ollama import Client, ResponseError

from .access import AccessChoice, WorkspaceAccess
from .agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    new_conversation,
    run_turn,
)
from .config import HOST, MODEL, MODELS


MODEL_DESCRIPTIONS = {
    "devstral-small-2:latest": "more capable; slower when it does not fit fully in GPU memory",
    "qwen3:8b": "faster and lighter; may be less capable on complex tasks",
}


def print_response(text: str) -> None:
    """Display each response fragment as it arrives."""
    print(text, end="", flush=True)


def print_tool_call(name: str) -> None:
    """Make tool use visible in the terminal."""
    print(f"\n[Tool: {name}]", flush=True)


def select_model(models: tuple[str, ...], default: str) -> str:
    """Ask which configured model to use for this session."""
    if len(models) == 1:
        return models[0]

    default_number = models.index(default) + 1
    print("Available models:")
    for number, model in enumerate(models, start=1):
        description = MODEL_DESCRIPTIONS.get(model)
        suffix = f" — {description}" if description else ""
        print(f"  {number}. {model}{suffix}")

    while True:
        choice = input(f"Choose a model [1-{len(models)}] (default {default_number}): ").strip()
        if not choice:
            return default
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        print(f"Enter a number from 1 to {len(models)}.")


def request_directory_access(directory: Path) -> AccessChoice:
    """Ask for one session-scoped filesystem permission decision."""
    print("\n[Permission required]")
    print(f"The agent wants to access a directory outside the workspace:\n  {directory}")
    print(f"  1 > Grant access to {directory} for this session")
    print("  2 > Grant full machine access for this session")
    print("  3 > Deny access and remember this decision")
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
    try:
        selected_model = select_model(MODELS, MODEL)
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye.")
        return
    client = Client(host=HOST)
    access = WorkspaceAccess(Path.cwd())
    messages = new_conversation()
    print(f"Using {selected_model} at {HOST}")
    print(f"Working directory: {Path.cwd()}")
    print("Commands: 'clear' resets the conversation; 'quit' exits.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break
        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            print("Goodbye.")
            break
        if user_input.lower() == "clear":
            messages = new_conversation()
            print("Conversation cleared.\n")
            continue

        turn_start = len(messages)
        messages.append({"role": "user", "content": user_input})
        print(flush=True)
        try:
            run_turn(
                client=client,
                model=selected_model,
                messages=messages,
                on_text=print_response,
                on_tool=print_tool_call,
                access=access,
                on_permission=request_directory_access,
            )
            print("\n")
        except ContextBudgetError as error:
            print(f"\n{error}\n")
        except EmptyModelResponseError as error:
            recover_turn(messages, turn_start)
            print(f"\nModel response failed: {error}\n")
        except ResponseError as error:
            recover_turn(messages, turn_start)
            print(f"\nOllama error: {error.status_code} — {error.error}\n")
        except KeyboardInterrupt:
            recover_turn(messages, turn_start)
            print("\nRequest cancelled.\n")
        except Exception as error:
            recover_turn(messages, turn_start)
            print(f"\nRequest failed: {error}\n")
