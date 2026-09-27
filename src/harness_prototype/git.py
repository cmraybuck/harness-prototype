"""Bounded, read-only inspection of the active workspace's Git changes."""

import os
from pathlib import Path
import subprocess

from .runtime_context import active_workspace
from .tool_results import tool_error, tool_success

MAX_STATUS_ENTRIES = 1_000
MAX_DIFF_CHARACTERS = 20_000
MAX_DIFF_FILES = 200
GIT_STATUS_TIMEOUT_SECONDS = 10


def _status_error(message: str, *, workspace: Path) -> dict:
    """Return a structured Git-status failure for the active workspace."""
    return tool_error(message, status="git_error", workspace=str(workspace))


def _run_git(arguments: list[str], *, workspace: Path) -> subprocess.CompletedProcess:
    """Run one local-only Git inspection command with index writes disabled."""
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(workspace), *arguments],
        capture_output=True, check=False, env=environment,
        timeout=GIT_STATUS_TIMEOUT_SECONDS,
    )


def _git_failure(result: subprocess.CompletedProcess, *, workspace: Path) -> dict:
    """Convert an unsuccessful Git command into a structured tool error."""
    detail = result.stderr.decode(errors="replace").strip()
    if not detail:
        detail = f"Git inspection failed with exit code {result.returncode}."
    return _status_error(detail, workspace=workspace)


def _workspace_pathspec(path: str | None, *, workspace: Path) -> str | None | dict:
    """Validate an optional workspace-relative path and return its Git pathspec."""
    if path is None:
        return None
    if not isinstance(path, str) or not path.strip():
        return _status_error("path must be a non-empty relative path.", workspace=workspace)
    candidate = Path(path)
    if candidate.is_absolute():
        return _status_error("path must be relative to the active workspace.", workspace=workspace)
    try:
        resolved = (workspace / candidate).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        return _status_error(f"Invalid path: {error}", workspace=workspace)
    if not resolved.is_relative_to(workspace):
        return _status_error("path cannot leave the active workspace.", workspace=workspace)
    return candidate.as_posix()


def _parse_numstat(output: bytes) -> list[dict]:
    """Parse NUL-delimited Git numstat output into per-file change counts."""
    records = output.split(b"\0")
    files = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        additions, deletions, raw_path = fields
        if raw_path:
            path = os.fsdecode(raw_path)
        elif index + 1 < len(records):
            original_path = os.fsdecode(records[index])
            path = os.fsdecode(records[index + 1])
            index += 2
            path = f"{original_path} -> {path}"
        else:
            continue
        files.append({
            "path": path,
            "additions": int(additions) if additions.isdigit() else None,
            "deletions": int(deletions) if deletions.isdigit() else None,
            "hunk_count": 0,
        })
    return files


def _add_hunk_counts(files: list[dict], patch: str) -> int:
    """Count patch hunks in total and assign them to files in Git output order."""
    file_index = -1
    hunk_count = 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            file_index += 1
        elif line.startswith("@@ "):
            hunk_count += 1
            if 0 <= file_index < len(files):
                files[file_index]["hunk_count"] += 1
    return hunk_count


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
    workspace = active_workspace()
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


def git_diff(mode: str = "combined", path: str | None = None) -> dict:
    """Return a bounded patch for local changes and list untracked paths.

    ``mode`` is ``unstaged``, ``staged``, or ``combined``. Combined output compares
    the working tree with HEAD, or the index in a repository without a commit. The
    optional path is relative to the active workspace and cannot leave it.
    Untracked file contents are not included in the patch.
    """
    workspace = active_workspace()
    if mode not in {"unstaged", "staged", "combined"}:
        return _status_error(
            "mode must be 'unstaged', 'staged', or 'combined'.", workspace=workspace,
        )
    pathspec = _workspace_pathspec(path, workspace=workspace)
    if isinstance(pathspec, dict):
        return pathspec
    suffix = ["--", pathspec] if pathspec is not None else []
    try:
        repository = _run_git(["rev-parse", "--is-inside-work-tree"], workspace=workspace)
        if repository.returncode != 0:
            return _git_failure(repository, workspace=workspace)
        diff_arguments = ["diff", "--no-ext-diff", "--no-textconv"]
        if mode == "staged":
            diff_arguments.append("--cached")
        elif mode == "combined":
            head = _run_git(["rev-parse", "--verify", "HEAD"], workspace=workspace)
            diff_arguments.append("HEAD" if head.returncode == 0 else "--cached")
        diff_result = _run_git([*diff_arguments, *suffix], workspace=workspace)
        if diff_result.returncode != 0:
            return _git_failure(diff_result, workspace=workspace)
        numstat_result = _run_git(
            [*diff_arguments, "--numstat", "-z", *suffix], workspace=workspace,
        )
        if numstat_result.returncode != 0:
            return _git_failure(numstat_result, workspace=workspace)
        untracked_result = _run_git(
            ["ls-files", "--others", "--exclude-standard", "-z", *suffix],
            workspace=workspace,
        )
        if untracked_result.returncode != 0:
            return _git_failure(untracked_result, workspace=workspace)
    except FileNotFoundError:
        return _status_error("Git is not installed or is not available on PATH.", workspace=workspace)
    except subprocess.TimeoutExpired:
        return _status_error(
            f"Git diff exceeded the {GIT_STATUS_TIMEOUT_SECONDS}-second limit.",
            workspace=workspace,
        )
    except OSError as error:
        return _status_error(str(error), workspace=workspace)

    full_patch = diff_result.stdout.decode(errors="replace")
    all_files = _parse_numstat(numstat_result.stdout)
    hunk_count = _add_hunk_counts(all_files, full_patch)
    additions = sum(item["additions"] or 0 for item in all_files)
    deletions = sum(item["deletions"] or 0 for item in all_files)
    binary_file_count = sum(item["additions"] is None for item in all_files)
    files = all_files[:MAX_DIFF_FILES]
    files_truncated = len(all_files) > len(files)
    patch = full_patch
    truncated = len(patch) > MAX_DIFF_CHARACTERS or files_truncated
    patch = patch[:MAX_DIFF_CHARACTERS]
    remaining_characters = MAX_DIFF_CHARACTERS - len(patch)
    untracked = []
    for raw_path in untracked_result.stdout.split(b"\0"):
        if not raw_path:
            continue
        decoded_path = os.fsdecode(raw_path)
        if len(decoded_path) > remaining_characters or len(untracked) >= MAX_STATUS_ENTRIES:
            truncated = True
            break
        untracked.append(decoded_path)
        remaining_characters -= len(decoded_path)
    return tool_success(
        complete=not truncated, truncated=truncated, workspace=str(workspace),
        mode=mode, path=pathspec, has_changes=bool(all_files), file_count=len(all_files),
        hunk_count=hunk_count, additions=additions, deletions=deletions,
        binary_file_count=binary_file_count, diff_characters=len(full_patch),
        files=files, files_truncated=files_truncated, diff=patch, untracked=untracked,
        untracked_contents_included=False,
    )
