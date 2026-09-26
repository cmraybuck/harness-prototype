"""Registration, risk categorization, and dispatch for model-facing tools."""

from inspect import signature

from .approval import ToolCategory
from .files import find_files, list_files, read_file, search_project
from .git import git_status
from .locations import get_location
from .tool_results import tool_error


# The same registry supplies model schemas and dispatches executable functions.
AVAILABLE_TOOLS = {
    "get_location": get_location,
    "list_files": list_files,
    "find_files": find_files,
    "search_project": search_project,
    "read_file": read_file,
    "git_status": git_status,
}

# Every registered tool must have an explicit category so policy lookup cannot
# silently fall back to a permissive default as new capabilities are added.
TOOL_CATEGORIES = {
    "get_location": ToolCategory.READ_ONLY,
    "list_files": ToolCategory.READ_ONLY,
    "find_files": ToolCategory.READ_ONLY,
    "search_project": ToolCategory.READ_ONLY,
    "read_file": ToolCategory.READ_ONLY,
    "git_status": ToolCategory.READ_ONLY,
}

if AVAILABLE_TOOLS.keys() != TOOL_CATEGORIES.keys():
    raise RuntimeError("Every available tool must declare exactly one approval category.")


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
