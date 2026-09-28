"""Session-scoped filesystem authorization for model-requested tools."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from .tool_results import tool_error


class AccessChoice(IntEnum):
    """Represent the user's response to an out-of-workspace access request."""

    GRANT_DIRECTORY = 1
    GRANT_ALL = 2
    DENY = 3
    GRANT_ONCE = 4


PermissionPrompt = Callable[[Path], AccessChoice]


@dataclass
class WorkspaceAccess:
    """Authorize filesystem tool targets for one application session."""

    workspace: Path
    granted_directories: list[Path] = field(default_factory=list)
    denied_directories: list[Path] = field(default_factory=list)
    full_access: bool = False

    def __post_init__(self) -> None:
        """Canonicalize the initial workspace and grant its complete subtree."""
        self.workspace = self.workspace.expanduser().resolve()
        self.granted_directories.append(self.workspace)

    def authorize_tool(
        self,
        name: str,
        arguments: object,
        prompt: PermissionPrompt,
        target_resolver: Callable[[object], list[Path] | dict | None] | None = None,
    ) -> dict | None:
        """Authorize a tool target or return a structured refusal result."""
        targets = (
            target_resolver(arguments)
            if target_resolver is not None
            else self._tool_directories(name, arguments)
        )
        if isinstance(targets, dict):
            return targets
        if targets is None or self.full_access:
            return None
        for target in targets:
            if self._overlaps_denial(target):
                return self._denied_result(target, remembered=True)
            if self._covered_by(target, self.granted_directories):
                continue

            choice = prompt(target)
            if choice == AccessChoice.GRANT_ONCE:
                continue
            if choice == AccessChoice.GRANT_ALL:
                self.full_access = True
                return None
            if choice == AccessChoice.GRANT_DIRECTORY:
                self.granted_directories.append(target)
                continue

            self.denied_directories.append(target)
            return self._denied_result(target, remembered=False)
        return None

    def _tool_directories(self, name: str, arguments: object) -> list[Path] | dict | None:
        """Return every directory whose subtree a filesystem tool would access."""
        if not isinstance(arguments, dict):
            return None
        argument_name = {
            "list_files": "path",
            "find_files": "root",
            "search_project": "path",
            "read_file": "path",
            "detect_project_technologies": "path",
        }.get(name)
        if argument_name is None:
            return None
        if argument_name not in arguments and name == "read_file":
            return None
        raw_path = arguments.get(argument_name, ".")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            candidate = Path(raw_path).expanduser()
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.workspace / candidate).resolve()
            )
            if name == "read_file" or (name == "search_project" and target.is_file()):
                return [target.parent]
            return [target]
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _covered_by(target: Path, directories: list[Path]) -> bool:
        """Report whether a canonical target is within any directory subtree."""
        return any(target == directory or target.is_relative_to(directory) for directory in directories)

    def _overlaps_denial(self, target: Path) -> bool:
        """Prevent broader requests from bypassing a remembered child denial."""
        return any(
            target == directory
            or target.is_relative_to(directory)
            or directory.is_relative_to(target)
            for directory in self.denied_directories
        )

    @staticmethod
    def _denied_result(target: Path, *, remembered: bool) -> dict:
        """Build a model-facing refusal that discourages repeated attempts."""
        detail = "Access was denied earlier in this session." if remembered else "The user denied access."
        return tool_error(
            f"{detail} Do not retry this directory or anything beneath it.",
            status="access_denied",
            directory=str(target),
        )
