"""Registration, risk categorization, and dispatch for model-facing tools."""

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from pathlib import Path

from .approval import ToolCategory
from .files import (
    MAX_FILE_CHANGES,
    apply_file_changes,
    file_change_target_directories,
    find_files,
    list_files,
    read_file,
    search_project,
)
from .git import git_status
from .locations import get_location
from .tool_results import tool_error


TargetResolver = Callable[[object], list[Path] | dict | None]


@dataclass(frozen=True)
class ToolSpec:
    """Register one function with its risk category and optional access resolver."""

    function: Callable[..., dict]
    category: ToolCategory
    target_resolver: TargetResolver | None = None
    model_schema: dict | None = None


FILE_CHANGES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "apply_file_changes",
        "description": (
            "Apply one preflighted batch of structured changes to UTF-8 files of any extension. "
            "Read an existing file first and use its revision for non-create actions."
        ),
        "parameters": {
            "type": "object",
            "required": ["base_path", "changes"],
            "properties": {
                "base_path": {
                    "type": "string",
                    "description": "Base directory for relative paths; use a get_location result or '.'.",
                },
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_FILE_CHANGES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "action", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["create", "overwrite", "append", "replace"],
                            },
                            "content": {"type": "string"},
                            "expected_revision": {
                                "type": ["string", "null"],
                                "description": (
                                    "Required for overwrite, append, and replace; "
                                    "omit for create."
                                ),
                            },
                            "old_text": {
                                "type": ["string", "null"],
                                "description": "Required only for replace and must occur exactly once.",
                            },
                        },
                    },
                },
            },
        },
    },
}


TOOL_SPECS = {
    "get_location": ToolSpec(get_location, ToolCategory.READ_ONLY),
    "list_files": ToolSpec(list_files, ToolCategory.READ_ONLY),
    "find_files": ToolSpec(find_files, ToolCategory.READ_ONLY),
    "search_project": ToolSpec(search_project, ToolCategory.READ_ONLY),
    "read_file": ToolSpec(read_file, ToolCategory.READ_ONLY),
    "git_status": ToolSpec(git_status, ToolCategory.READ_ONLY),
    "apply_file_changes": ToolSpec(
        apply_file_changes,
        ToolCategory.WRITE,
        file_change_target_directories,
        FILE_CHANGES_SCHEMA,
    ),
}

# Derived views retain the simple interfaces used for schemas and policy lookup.
AVAILABLE_TOOLS = {name: spec.function for name, spec in TOOL_SPECS.items()}
TOOL_CATEGORIES = {name: spec.category for name, spec in TOOL_SPECS.items()}
TOOL_TARGET_RESOLVERS = {
    name: spec.target_resolver
    for name, spec in TOOL_SPECS.items()
    if spec.target_resolver is not None
}
MODEL_TOOLS = [spec.model_schema or spec.function for spec in TOOL_SPECS.values()]


def execute_tool(name: str, arguments: dict) -> dict:
    """Validate a request before calling a registered tool."""
    tool = AVAILABLE_TOOLS.get(name)
    if tool is None:
        return tool_error(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return tool_error("Tool arguments must be an object.")
    try:
        signature(tool).bind(**arguments)
    except TypeError as error:
        return tool_error(str(error))
    return tool(**arguments)
