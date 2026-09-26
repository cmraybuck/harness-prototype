"""Shared result contracts for model-facing tools."""

from typing import Any


def tool_success(
    *,
    complete: bool = True,
    truncated: bool = False,
    **payload: Any,
) -> dict:
    """Build a successful tool result with the common status fields."""
    return {
        **payload,
        "ok": True,
        "error": None,
        "complete": complete,
        "truncated": truncated,
    }


def tool_error(error: str, **payload: Any) -> dict:
    """Build an expected tool failure with the common status fields."""
    return {
        **payload,
        "ok": False,
        "error": error,
        "complete": False,
        "truncated": False,
    }
