"""Registration, risk categorization, and dispatch for model-facing tools."""

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from pathlib import Path

from .approval import ToolCategory
from .execution import MAX_TEST_TARGETS, run_tests
from .files import (
    MAX_FILE_CHANGES,
    apply_file_changes,
    file_change_target_directories,
    find_files,
    list_files,
    read_file,
    search_project,
)
from .git import git_diff, git_status
from .locations import get_location
from .project_detection import detect_project_technologies
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

RUN_TESTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": (
            "Run the configured full test suite or selected test targets inside the "
            "active workspace. This executes project code and requires execution approval."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "targets": {
                    "type": ["array", "null"],
                    "minItems": 1,
                    "maxItems": MAX_TEST_TARGETS,
                    "items": {"type": "string"},
                    "description": (
                        "Optional configured-runner selectors. Omit for the full suite; "
                        "do not pass command-line options."
                    ),
                },
            },
        },
    },
}

SEARCH_PROJECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_project",
        "description": (
            "Search project text in one bounded pass. Exact matches lead, while "
            "case-insensitive and whitespace-flexible candidates remain visible. "
            "Inspect match_quality, match_counts, fallback_used, and completeness."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Nonempty single-line text or stable source anchor.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search; defaults to the workspace.",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional case-insensitive filename wildcard such as *.py.",
                },
            },
        },
    },
}


TOOL_SPECS = {
    "get_location": ToolSpec(get_location, ToolCategory.READ_ONLY),
    "list_files": ToolSpec(list_files, ToolCategory.READ_ONLY),
    "find_files": ToolSpec(find_files, ToolCategory.READ_ONLY),
    "search_project": ToolSpec(
        search_project,
        ToolCategory.READ_ONLY,
        model_schema=SEARCH_PROJECT_SCHEMA,
    ),
    "read_file": ToolSpec(read_file, ToolCategory.READ_ONLY),
    "git_status": ToolSpec(git_status, ToolCategory.READ_ONLY),
    "git_diff": ToolSpec(git_diff, ToolCategory.READ_ONLY),
    "detect_project_technologies": ToolSpec(
        detect_project_technologies,
        ToolCategory.READ_ONLY,
    ),
    "run_tests": ToolSpec(
        run_tests,
        ToolCategory.EXECUTION,
        model_schema=RUN_TESTS_SCHEMA,
    ),
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
