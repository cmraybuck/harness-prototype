"""Registration, risk categorization, and dispatch for model-facing tools."""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from inspect import Parameter, getdoc, signature
from pathlib import Path
from types import UnionType
from typing import get_args, get_origin

from .approval import ToolCategory
from .config import CHECKS
from .execution import MAX_COMMAND_ARGUMENTS, MAX_TEST_TARGETS, run_checks, run_command, run_tests
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
    """Register one function with its safety, schema, access, and limit metadata."""

    function: Callable[..., dict]
    category: ToolCategory
    target_resolver: TargetResolver | None = None
    model_schema: dict | None = None
    material_limits: tuple[str, ...] = ()


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
            "additionalProperties": False,
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
                    "default": None,
                },
            },
        },
    },
}

_CHECK_NAMES = list(CHECKS)
_CHECK_TARGET_SUMMARY = "; ".join(
    f"{name}: {', '.join(check.targets) if check.targets else 'no targets'}"
    for name, check in CHECKS.items()
) or "none configured"

RUN_CHECKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_checks",
        "description": (
            "Run an exactly configured build, lint, formatting, type-check, or other "
            "project check inside the active workspace. This executes project code "
            "and requires execution approval. Configured checks and targets: "
            f"{_CHECK_TARGET_SUMMARY}."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact configured check name.",
                    **({"enum": _CHECK_NAMES} if _CHECK_NAMES else {}),
                },
                "target": {
                    "type": ["string", "null"],
                    "description": "Optional exact target name configured for this check.",
                    "default": None,
                },
            },
        },
    },
}

RUN_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run one shell-free engineering command in the active workspace. The "
            "executable must be configured as allowed; shells are always denied. "
            "This broad capability executes project code and requires execution approval."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {
                    "type": "array", "minItems": 1,
                    "maxItems": MAX_COMMAND_ARGUMENTS,
                    "items": {"type": "string"},
                    "description": "Bare executable name followed by separate arguments; no shell syntax.",
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
                    "default": ".",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional case-insensitive filename wildcard such as *.py.",
                    "default": "*",
                },
            },
        },
    },
}


TOOL_SPECS = {
    "get_location": ToolSpec(
        get_location, ToolCategory.READ_ONLY,
        material_limits=("named standard locations only",),
    ),
    "list_files": ToolSpec(
        list_files, ToolCategory.READ_ONLY,
        material_limits=("1-100 results per page", "20,000 directory entries"),
    ),
    "find_files": ToolSpec(
        find_files, ToolCategory.READ_ONLY,
        material_limits=("1-100 results", "20,000 entries", "10-second search"),
    ),
    "search_project": ToolSpec(
        search_project,
        ToolCategory.READ_ONLY,
        model_schema=SEARCH_PROJECT_SCHEMA,
        material_limits=(
            "1-100 results (model schema fixes the default of 100)",
            "20,000 entries", "10-second search", "1 MiB UTF-8 files",
            "8,000 result characters",
        ),
    ),
    "read_file": ToolSpec(
        read_file, ToolCategory.READ_ONLY,
        material_limits=("1 MiB UTF-8 files", "1-200 lines", "8,000 output characters"),
    ),
    "git_status": ToolSpec(
        git_status, ToolCategory.READ_ONLY,
        material_limits=("active workspace only", "1,000 entries", "10-second timeout"),
    ),
    "git_diff": ToolSpec(
        git_diff, ToolCategory.READ_ONLY,
        material_limits=(
            "active workspace only", "200 files", "20,000 patch characters",
            "10-second timeout", "untracked contents excluded",
        ),
    ),
    "detect_project_technologies": ToolSpec(
        detect_project_technologies,
        ToolCategory.READ_ONLY,
        material_limits=(
            "20,000 entries", "100 manifests", "256 KiB per manifest",
            "50 scripts per project",
        ),
    ),
    "run_tests": ToolSpec(
        run_tests,
        ToolCategory.EXECUTION,
        model_schema=RUN_TESTS_SCHEMA,
        material_limits=(
            "configured command only", "1-50 optional targets",
            "500 characters per target", "configured timeout and output bounds",
        ),
    ),
    "run_checks": ToolSpec(
        run_checks,
        ToolCategory.EXECUTION,
        model_schema=RUN_CHECKS_SCHEMA,
        material_limits=(
            "configured commands only", "optional configured target only",
            "configured timeout and output bounds",
        ),
    ),
    "run_command": ToolSpec(
        run_command,
        ToolCategory.EXECUTION,
        model_schema=RUN_COMMAND_SCHEMA,
        material_limits=(
            "active workspace working directory", "configured executable allowlist",
            "shell executables always denied", "1-100 arguments",
            "configured timeout and output bounds",
        ),
    ),
    "apply_file_changes": ToolSpec(
        apply_file_changes,
        ToolCategory.WRITE,
        file_change_target_directories,
        FILE_CHANGES_SCHEMA,
        material_limits=(
            "1-100 files", "1 MiB UTF-8 files",
            "preflighted batch with best-effort rollback",
        ),
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


def _parameter_details(parameters: dict) -> str:
    """Render explicit requiredness and defaults from one parameter schema."""
    properties = parameters.get("properties", {})
    required_names = list(parameters.get("required", ()))
    required = ", ".join(required_names) or "none"
    optional = []
    for name, property_schema in properties.items():
        if name in required_names:
            continue
        if "default" in property_schema:
            default = json.dumps(property_schema["default"], ensure_ascii=False)
            optional.append(f"{name} (default {default})")
        else:
            optional.append(f"{name} (no default declared)")
    optional_text = ", ".join(optional) or "none"
    return (
        f"Required parameters (report exactly): {required}. "
        f"Optional parameters (report exactly): {optional_text}."
    )


def _capability_details(spec: ToolSpec, parameters: dict) -> str:
    """Render parameter, safety, and limit details shared by every model schema."""
    limits = "; ".join(spec.material_limits) or "none declared"
    return (
        f"{_parameter_details(parameters)} Safety category: {spec.category.value}. "
        f"Material limits (report all verbatim): {limits}."
    )


def _argument_descriptions(function: Callable[..., dict]) -> dict[str, str]:
    """Extract concise parameter descriptions from a function's Args section."""
    descriptions: dict[str, str] = {}
    parameter_names = set(signature(function).parameters)
    current_name: str | None = None
    in_arguments = False
    arguments_complete = False
    for line in (getdoc(function) or "").splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_arguments = True
            continue
        if in_arguments and not stripped and current_name is not None:
            arguments_complete = True
            continue
        if arguments_complete and stripped:
            break
        if not in_arguments or not stripped:
            continue
        if ":" in stripped:
            name, description = stripped.split(":", 1)
            if name in parameter_names:
                current_name = name
                descriptions[name] = description.strip()
                continue
        if current_name is not None:
            descriptions[current_name] += " " + stripped
    return descriptions


def _annotation_schema(annotation: object) -> dict:
    """Convert supported Python annotations into deterministic JSON-schema fields."""
    if annotation is Parameter.empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (list, tuple):
        item_annotation = arguments[0] if arguments else str
        return {"type": "array", "items": _annotation_schema(item_annotation)}
    if origin in (UnionType,):
        member_schemas = [_annotation_schema(item) for item in arguments]
        non_null = [item for item in member_schemas if item.get("type") != "null"]
        if len(non_null) == 1 and len(member_schemas) == 2:
            schema = non_null[0]
            schema["type"] = [schema["type"], "null"]
            return schema
        return {"anyOf": member_schemas}
    scalar_types = {
        str: "string", int: "integer", float: "number", bool: "boolean",
        type(None): "null",
    }
    return {"type": scalar_types.get(annotation, "object")}


def _callable_schema(spec: ToolSpec) -> dict:
    """Generate one explicit schema from a registered callable's interface."""
    function_signature = signature(spec.function)
    descriptions = _argument_descriptions(spec.function)
    properties = {}
    required = []
    for name, parameter in function_signature.parameters.items():
        property_schema = _annotation_schema(parameter.annotation)
        if name in descriptions:
            property_schema["description"] = descriptions[name]
        if parameter.default is Parameter.empty:
            required.append(name)
        else:
            property_schema["default"] = parameter.default
        properties[name] = property_schema
    purpose = (getdoc(spec.function) or "").split("\n\n", 1)[0].replace("\n", " ")
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": spec.function.__name__,
            "description": f"{purpose} {_capability_details(spec, parameters)}",
            "parameters": parameters,
        },
    }


def _model_tool(spec: ToolSpec) -> dict:
    """Build one explicit model schema without changing executable dispatch."""
    if spec.model_schema is not None:
        schema = deepcopy(spec.model_schema)
        description = str(schema["function"]["description"]).rstrip()
        details = _capability_details(spec, schema["function"]["parameters"])
        schema["function"]["description"] = f"{description} {details}"
        return schema
    return _callable_schema(spec)


MODEL_TOOLS = [_model_tool(spec) for spec in TOOL_SPECS.values()]


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
