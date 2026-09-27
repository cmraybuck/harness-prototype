"""Conversation-local execution context for backend tool calls."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_workspace: ContextVar[Path | None] = ContextVar("flux_workspace", default=None)
_conversation_id: ContextVar[str | None] = ContextVar("flux_conversation_id", default=None)


@contextmanager
def conversation_context(workspace: Path, conversation_id: str) -> Iterator[None]:
    """Bind workspace and ownership to the current turn execution context."""
    workspace_token = _workspace.set(workspace.resolve())
    conversation_token = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _conversation_id.reset(conversation_token)
        _workspace.reset(workspace_token)


def active_workspace() -> Path:
    """Return the turn workspace or the process directory for legacy callers."""
    return _workspace.get() or Path.cwd().resolve()


def active_conversation_id() -> str | None:
    """Return the owning conversation when a tool runs through a runtime."""
    return _conversation_id.get()


def resolve_path(path: str | Path) -> Path:
    """Resolve an absolute path or a relative path beneath the active workspace."""
    candidate = Path(path).expanduser()
    return (
        candidate.resolve()
        if candidate.is_absolute()
        else (active_workspace() / candidate).resolve()
    )
