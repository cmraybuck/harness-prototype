"""Interactive terminal entry point."""

from pathlib import Path

from ollama import Client, ResponseError

from .agent_loop import (
    ContextBudgetError,
    EmptyModelResponseError,
    new_conversation,
    run_turn,
)
from .config import HOST, MODEL


def print_response(text: str) -> None:
    """Display each response fragment as it arrives."""
    print(text, end="", flush=True)


def print_tool_call(name: str) -> None:
    """Make tool use visible in the terminal."""
    print(f"\n[Tool: {name}]", flush=True)


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
    client = Client(host=HOST)
    messages = new_conversation()
    print(f"Using {MODEL} at {HOST}")
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
                model=MODEL,
                messages=messages,
                on_text=print_response,
                on_tool=print_tool_call,
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
