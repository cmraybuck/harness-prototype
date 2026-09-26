"""Bounded filesystem operations used by the agent tools."""

import fnmatch
import json
import os
import time
from collections import deque
from itertools import islice
from pathlib import Path

from .tool_results import tool_error, tool_success

MAX_FILE_BYTES = 1_048_576
MAX_OUTPUT_CHARS = 8_000
MAX_ENTRIES = 20_000
SEARCH_SECONDS = 10
SEARCH_OUTPUT_CHARS = 8_000
EXCLUDED_SEARCH_DIRECTORIES = {
    ".git", ".hg", ".mypy_cache", ".next", ".pytest_cache", ".ruff_cache",
    ".svn", ".tox", ".venv", "__pycache__", "build", "coverage", "dist",
    "node_modules", "target", "venv",
}


def list_files(offset: int = 0, max_entries: int = 100, path: str = ".") -> dict:
    """List a bounded sorted page of immediate names in a directory."""
    if type(offset) is not int or offset < 0:
        return tool_error("offset must be a nonnegative integer.")
    if type(max_entries) is not int or not 1 <= max_entries <= 100:
        return tool_error("max_entries must be between 1 and 100.")
    if not isinstance(path, str) or not path.strip():
        return tool_error("path must be a nonempty directory path.")
    try:
        directory = Path(path).expanduser().resolve()
        with os.scandir(directory) as iterator:
            entries = list(islice(iterator, MAX_ENTRIES + 1))
        if len(entries) > MAX_ENTRIES:
            return tool_error(
                "Directory exceeds the 20000-entry listing limit; choose a narrower path."
            )
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        page, size = [], 0
        for entry in entries[offset:offset + max_entries]:
            name = entry.name + ("/" if entry.is_dir() else "")
            encoded_size = len(json.dumps(name)) + 2
            if size + encoded_size > 4000:
                break
            page.append(name)
            size += encoded_size
        next_offset = offset + len(page)
        truncated = next_offset < len(entries)
        return tool_success(
            complete=not truncated,
            truncated=truncated,
            directory=str(directory),
            entries=page,
            next_offset=next_offset if truncated else None,
        )
    except (OSError, ValueError) as error:
        return tool_error(str(error))


def find_files(pattern: str, root: str = ".", max_results: int = 100) -> dict:
    """Recursively find files by case-insensitive filename pattern.

    A plain name without an extension first looks for that exact filename. If
    there are no exact matches, files with the same name and any extension are
    returned instead, with extension_inferred set to true. Wildcard and
    extension-bearing patterns retain ordinary filename matching semantics.
    """
    if not isinstance(pattern, str) or not pattern or any(c in pattern for c in "/\\"):
        return tool_error("pattern must be a nonempty filename or wildcard, not a path.")
    if not isinstance(root, str) or not root.strip():
        return tool_error("root must be a nonempty directory path.")
    if type(max_results) is not int or not 1 <= max_results <= 100:
        return tool_error("max_results must be an integer between 1 and 100.")
    try:
        directory = Path(root).expanduser().resolve()
        if not directory.is_dir():
            return tool_error(f"Not a directory: {directory}")
    except (OSError, ValueError) as error:
        return tool_error(str(error))
    infer_extension = "." not in pattern and not any(character in pattern for character in "*?[")
    result = tool_success(
        root=str(directory),
        matches=[],
        extension_inferred=False,
        stop_reason=None,
        skipped_links=0,
        errors=[],
        error_count=0,
        errors_truncated=False,
    )
    inferred_matches = []
    inferred_limit_reached = False

    def record_error(error: OSError) -> None:
        """Record a bounded diagnostic while retaining the full error count."""
        result["complete"] = False
        result["error_count"] += 1
        if len(result["errors"]) < 20:
            result["errors"].append(str(error))
        else:
            result["errors_truncated"] = True

    def apply_inferred_matches() -> None:
        """Expose collected fallback matches when no exact name matched."""
        if not result["matches"] and inferred_matches:
            result["matches"] = inferred_matches
            result["extension_inferred"] = True
            if inferred_limit_reached and result["stop_reason"] is None:
                result.update(complete=False, truncated=True, stop_reason="result_limit")

    pending = deque([directory])
    deadline = time.monotonic() + SEARCH_SECONDS
    visited = 0
    while pending:
        if time.monotonic() >= deadline:
            result.update(complete=False, truncated=True, stop_reason="time_limit")
            apply_inferred_matches()
            return result
        parent = pending.popleft()
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        result.update(complete=False, truncated=True, stop_reason="entry_limit")
                        apply_inferred_matches()
                        return result
                    path = Path(entry.path)
                    try:
                        if entry.is_symlink() or path.is_junction():
                            result["skipped_links"] += 1
                            result["complete"] = False
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            folded_name = entry.name.casefold()
                            folded_pattern = pattern.casefold()
                            if fnmatch.fnmatchcase(folded_name, folded_pattern):
                                result["matches"].append(str(path))
                                if len(result["matches"]) >= max_results:
                                    result.update(complete=False, truncated=True, stop_reason="result_limit")
                                    return result
                            elif infer_extension and folded_name.startswith(folded_pattern + "."):
                                if len(inferred_matches) < max_results:
                                    inferred_matches.append(str(path))
                                else:
                                    inferred_limit_reached = True
                    except OSError as error:
                        record_error(error)
        except OSError as error:
            record_error(error)
    apply_inferred_matches()
    return result


def search_project(
    text: str,
    path: str = ".",
    file_pattern: str = "*",
    max_results: int = 100,
) -> dict:
    """Search nested UTF-8 project files for literal text.

    Args:
        text: Nonempty, case-sensitive literal text to find within a source line.
        path: Workspace-relative file or directory to search. Absolute paths and
            paths outside the process working directory are rejected.
        file_pattern: Optional case-insensitive filename wildcard, such as
            ``*.py``. It must be a filename pattern rather than a path.
        max_results: Maximum matching lines to return, between 1 and 100.

    Results include workspace-relative paths, one-based line numbers, and line
    excerpts. Git metadata, common dependency/generated directories, links,
    binary or non-UTF-8 files, and files over 1 MiB are skipped. Traversal,
    runtime, result count, and JSON-encoded result text are bounded; ``complete``
    and ``stop_reason`` distinguish exhaustive searches from partial results.
    """
    if not isinstance(text, str) or not text:
        return tool_error("text must be a nonempty string.")
    if "\n" in text or "\r" in text:
        return tool_error("text must not contain a newline.")
    if not isinstance(path, str) or not path.strip():
        return tool_error("path must be a nonempty workspace-relative path.")
    if Path(path).is_absolute():
        return tool_error("path must be relative to the working directory.")
    if (not isinstance(file_pattern, str) or not file_pattern
            or any(character in file_pattern for character in "/\\")):
        return tool_error("file_pattern must be a nonempty filename wildcard, not a path.")
    if type(max_results) is not int or not 1 <= max_results <= 100:
        return tool_error("max_results must be an integer between 1 and 100.")

    try:
        workspace = Path.cwd().resolve()
        search_path = (workspace / path).resolve()
        search_path.relative_to(workspace)
        if not search_path.exists():
            return tool_error(f"Path does not exist: {search_path}")
        if not search_path.is_file() and not search_path.is_dir():
            return tool_error(f"Not a regular file or directory: {search_path}")
    except (OSError, ValueError) as error:
        return tool_error(str(error))

    result = tool_success(
        workspace=str(workspace),
        path=search_path.relative_to(workspace).as_posix() or ".",
        matches=[],
        stop_reason=None,
        files_searched=0,
        skipped_files={"excluded": 0, "binary_or_non_utf8": 0, "too_large": 0, "links": 0},
        errors=[],
        error_count=0,
        errors_truncated=False,
    )
    deadline = time.monotonic() + SEARCH_SECONDS
    visited = 0
    output_size = 0
    folded_pattern = file_pattern.casefold()
    pending = deque([search_path])

    def stop(reason: str) -> dict:
        """Mark the search partial and return its accumulated evidence."""
        result.update(complete=False, truncated=True, stop_reason=reason)
        return result

    def record_error(path_with_error: Path, error: OSError) -> None:
        """Record a bounded diagnostic while retaining the full error count."""
        result["complete"] = False
        result["error_count"] += 1
        if len(result["errors"]) < 20:
            result["errors"].append(f"{path_with_error}: {error}")
        else:
            result["errors_truncated"] = True

    while pending:
        if time.monotonic() >= deadline:
            return stop("time_limit")
        current = pending.popleft()
        try:
            if current.is_symlink() or current.is_junction():
                result["skipped_files"]["links"] += 1
                result["complete"] = False
                continue
            relative_parts = current.relative_to(workspace).parts
            directory_parts = relative_parts if current.is_dir() else relative_parts[:-1]
            if any(part.casefold() in EXCLUDED_SEARCH_DIRECTORIES for part in directory_parts):
                result["skipped_files"]["excluded"] += 1
                continue
            if current.is_dir():
                with os.scandir(current) as entries:
                    children = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
                for entry in children:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        return stop("entry_limit")
                    pending.append(Path(entry.path))
                continue
            if not fnmatch.fnmatchcase(current.name.casefold(), folded_pattern):
                continue
            if current.stat().st_size > MAX_FILE_BYTES:
                result["skipped_files"]["too_large"] += 1
                continue
            data = current.read_bytes()
            if b"\x00" in data:
                result["skipped_files"]["binary_or_non_utf8"] += 1
                continue
            try:
                content = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                result["skipped_files"]["binary_or_non_utf8"] += 1
                continue
            result["files_searched"] += 1
            lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for line_number, line in enumerate(lines, 1):
                if text not in line:
                    continue
                match_index = line.index(text)
                excerpt_start = max(0, match_index - 200)
                excerpt_end = min(len(line), match_index + len(text) + 200)
                excerpt = line[excerpt_start:excerpt_end]
                if excerpt_start:
                    excerpt = "…" + excerpt
                if excerpt_end < len(line):
                    excerpt += "…"
                match = {
                    "path": current.relative_to(workspace).as_posix(),
                    "line_number": line_number,
                    "excerpt": excerpt,
                }
                encoded_size = len(json.dumps(match, ensure_ascii=False))
                if output_size + encoded_size > SEARCH_OUTPUT_CHARS:
                    return stop("output_limit")
                result["matches"].append(match)
                output_size += encoded_size
                if len(result["matches"]) >= max_results:
                    return stop("result_limit")
        except OSError as error:
            record_error(current, error)
    return result


def read_file(path: str, start_line: int = 1, max_lines: int = 100) -> dict:
    """Read a page of a UTF-8 text file, with one-based line numbers.

    Args:
        path: Absolute file path, or a path relative to the working directory.
            Accepts paths returned by find_files.
        start_line: First line to read, starting at 1.
        max_lines: Maximum lines to return, between 1 and 200.

    Files must be at most 1 MiB. Output is capped at 8000 characters. Use
    next_line to request another page when truncated is true. Binary and
    non-UTF-8 files return an error rather than guessed text.
    """
    if not isinstance(path, str) or not path.strip():
        return tool_error("path must be a nonempty file path.")
    if type(start_line) is not int or start_line < 1:
        return tool_error("start_line must be a positive integer.")
    if type(max_lines) is not int or not 1 <= max_lines <= 200:
        return tool_error("max_lines must be an integer between 1 and 200.")

    try:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            return tool_error(f"Not a regular file: {file_path}")
        with file_path.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return tool_error("File exceeds the 1 MiB read limit.")
        if b"\x00" in data:
            return tool_error("Binary or non-UTF-8 file is not supported.")
        # Only CR, LF, and CRLF delimit source lines; preserve other characters.
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n") if text else []
        if text.endswith("\n"):
            lines.pop()
    except UnicodeDecodeError:
        return tool_error("File is not valid UTF-8 text.")
    except (OSError, ValueError) as error:
        return tool_error(str(error))

    content = []
    size = 0
    for index in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
        numbered_line = f"{index + 1}: {lines[index]}\n"
        if size + len(numbered_line) > MAX_OUTPUT_CHARS:
            if not content:
                return tool_error(f"Line {index + 1} exceeds the output limit.")
            break
        content.append(numbered_line)
        size += len(numbered_line)

    next_line = start_line + len(content)
    truncated = next_line <= len(lines)
    return tool_success(
        complete=not truncated,
        truncated=truncated,
        path=str(file_path),
        start_line=start_line,
        lines_read=len(content),
        total_lines=len(lines),
        content="".join(content),
        next_line=next_line if truncated else None,
    )
