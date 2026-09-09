"""Read-only tools exposed to the model."""

from inspect import signature

from .files import find_files, list_files, read_file
from .locations import get_location


# The same registry supplies model schemas and dispatches executable functions.
AVAILABLE_TOOLS = {
    "get_location": get_location,
    "list_files": list_files,
    "find_files": find_files,
    "read_file": read_file,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """Validate a request before calling a registered tool."""
    tool = AVAILABLE_TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "Tool arguments must be an object."}
    try:
        signature(tool).bind(**arguments)
    except TypeError as error:
        return {"ok": False, "error": str(error)}
    return tool(**arguments)
