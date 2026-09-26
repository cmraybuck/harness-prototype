"""Bounded, read-only inspection of the active workspace's Git status."""

import os
from pathlib import Path
import subprocess

from .tool_results import tool_error, tool_success

MAX_STATUS_ENTRIES = 1_000
GIT_STATUS_TIMEOUT_SECONDS = 10


def _status_error(message: str, *, workspace: Path) -> dict:
    """Return a structured Git-status failure for the active workspace."""
    return tool_error(message, status="git_error", workspace=str(workspace))


def _decode_path(raw_path: bytes, workspace_prefix: str) -> str:
    """Decode a raw Git path and make it relative to the active workspace."""
    path = os.fsdecode(raw_path)
    if workspace_prefix and path.startswith(workspace_prefix):
        return path[len(workspace_prefix):]
    return path


def _append_path(categories: dict[str, list], category: str, path: str) -> None:
    """Append one path while preserving Git's stable status order."""
    categories[category].append(path)


def _parse_status(output: bytes, workspace: Path, workspace_prefix: str = "") -> dict:
    """Parse NUL-delimited porcelain-v2 output into user-facing categories."""
    categories: dict[str, list] = {
        "staged": [], "unstaged": [], "untracked": [], "renamed": [],
        "deleted": [], "conflicted": [],
    }
    records = output.split(b"\0")
    if records and not records[-1]:
        records.pop()

    entry_count = 0
    index = 0
    while index < len(records):
        record = records[index]
        if not record:
            return _status_error("Git returned an empty status record.", workspace=workspace)
        record_type = record[:1]
        fields = record.split(b" ", 1)
        if record_type == b"?" and len(fields) == 2:
            _append_path(categories, "untracked", _decode_path(fields[1], workspace_prefix))
        elif record_type in {b"1", b"2", b"u"}:
            expected_fields = {b"1": 9, b"2": 10, b"u": 11}[record_type]
            fields = record.split(b" ", expected_fields - 1)
            if len(fields) != expected_fields or len(fields[1]) != 2:
                return _status_error("Git returned a malformed status record.", workspace=workspace)
            index_status = chr(fields[1][0])
            worktree_status = chr(fields[1][1])
            path = _decode_path(fields[-1], workspace_prefix)
            if record_type == b"u":
                _append_path(categories, "conflicted", path)
            else:
                if index_status != ".":
                    _append_path(categories, "staged", path)
                if worktree_status != ".":
                    _append_path(categories, "unstaged", path)
                if index_status == "D" or worktree_status == "D":
                    _append_path(categories, "deleted", path)
                if index_status == "R" or worktree_status == "R":
                    if record_type != b"2" or index + 1 >= len(records):
                        return _status_error("Git returned an incomplete rename record.", workspace=workspace)
                    index += 1
                    categories["renamed"].append({
                        "path": path,
                        "original_path": _decode_path(records[index], workspace_prefix),
                        "index_status": index_status,
                        "worktree_status": worktree_status,
                    })
        else:
            return _status_error("Git returned an unsupported status record.", workspace=workspace)

        entry_count += 1
        index += 1
        if entry_count >= MAX_STATUS_ENTRIES and index < len(records):
            return tool_success(
                complete=False, truncated=True, workspace=str(workspace), clean=False,
                entry_count=entry_count, **categories,
            )

    return tool_success(
        workspace=str(workspace), clean=entry_count == 0,
        entry_count=entry_count, **categories,
    )


def git_status() -> dict:
    """Report bounded Git status for the active workspace without changing it.

    Paths are relative to the process workspace. Results categorize staged,
    unstaged, untracked, renamed, deleted, and conflicted paths. A path may occur
    in more than one category. At most 1000 status entries are returned. The
    command does not access a remote and disables Git's optional index refresh.
    """
    workspace = Path.cwd().resolve()
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    prefix_command = [
        "git", "--no-optional-locks", "-C", str(workspace),
        "rev-parse", "--show-prefix",
    ]
    status_command = [
        "git", "--no-optional-locks", "-C", str(workspace), "status",
        "--porcelain=v2", "-z", "--untracked-files=all", "--", ".",
    ]
    try:
        prefix_result = subprocess.run(
            prefix_command, capture_output=True, check=False, env=environment,
            timeout=GIT_STATUS_TIMEOUT_SECONDS,
        )
        completed = subprocess.run(
            status_command, capture_output=True, check=False, env=environment,
            timeout=GIT_STATUS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _status_error("Git is not installed or is not available on PATH.", workspace=workspace)
    except subprocess.TimeoutExpired:
        return _status_error(
            f"Git status exceeded the {GIT_STATUS_TIMEOUT_SECONDS}-second limit.",
            workspace=workspace,
        )
    except OSError as error:
        return _status_error(str(error), workspace=workspace)

    failed = prefix_result if prefix_result.returncode != 0 else completed
    if failed.returncode != 0:
        detail = failed.stderr.decode(errors="replace").strip()
        if not detail:
            detail = f"Git status failed with exit code {failed.returncode}."
        return _status_error(detail, workspace=workspace)
    workspace_prefix = os.fsdecode(prefix_result.stdout).removesuffix("\n").removesuffix("\r")
    return _parse_status(completed.stdout, workspace, workspace_prefix)
