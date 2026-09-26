"""Bounded filesystem operations used by the agent tools."""

import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .tool_results import tool_error, tool_success

MAX_FILE_BYTES = 1_048_576
MAX_OUTPUT_CHARS = 8_000
MAX_ENTRIES = 20_000
MAX_FILE_CHANGES = 100
SEARCH_SECONDS = 10
SEARCH_OUTPUT_CHARS = 8_000
EXCLUDED_SEARCH_DIRECTORIES = {
    ".git", ".hg", ".mypy_cache", ".next", ".pytest_cache", ".ruff_cache",
    ".svn", ".tox", ".venv", "__pycache__", "build", "coverage", "dist",
    "node_modules", "target", "venv",
}


@dataclass(frozen=True)
class FileChange:
    """Hold one validated structured file change."""

    path: str
    action: str
    content: str
    expected_revision: str | None
    old_text: str | None


def _parse_file_changes(changes: object) -> tuple[list[FileChange] | None, str | None]:
    """Validate structured file changes without accessing the filesystem."""
    if not isinstance(changes, list) or not changes:
        return None, "changes must be a nonempty array."
    if len(changes) > MAX_FILE_CHANGES:
        return None, f"changes exceeds the {MAX_FILE_CHANGES}-file limit."
    parsed = []
    allowed_keys = {"path", "action", "content", "expected_revision", "old_text"}
    for index, value in enumerate(changes):
        if not isinstance(value, dict):
            return None, f"changes[{index}] must be an object."
        unexpected_keys = set(value) - allowed_keys
        if unexpected_keys:
            names = ", ".join(sorted(str(key) for key in unexpected_keys))
            return None, f"changes[{index}] has unexpected fields: {names}."
        path = value.get("path")
        action = value.get("action")
        content = value.get("content")
        expected_revision = value.get("expected_revision")
        old_text = value.get("old_text")
        if not isinstance(path, str) or not path.strip():
            return None, f"changes[{index}].path must be a nonempty string."
        if action not in {"create", "overwrite", "append", "replace"}:
            return None, (
                f"changes[{index}].action must be create, overwrite, append, or replace."
            )
        if not isinstance(content, str):
            return None, f"changes[{index}].content must be a string."
        if expected_revision is not None and not isinstance(expected_revision, str):
            return None, f"changes[{index}].expected_revision must be a string or null."
        if old_text is not None and not isinstance(old_text, str):
            return None, f"changes[{index}].old_text must be a string or null."
        if action == "create":
            if expected_revision is not None:
                return None, f"changes[{index}].expected_revision must be null for create."
            if old_text is not None:
                return None, f"changes[{index}].old_text must be null for create."
        else:
            if not expected_revision:
                return None, f"changes[{index}].expected_revision is required for {action}."
            revision_value = expected_revision.removeprefix("sha256:")
            if (
                not expected_revision.startswith("sha256:")
                or len(revision_value) != 64
                or any(character not in "0123456789abcdef" for character in revision_value)
            ):
                return None, f"changes[{index}].expected_revision must be a read_file SHA-256 revision."
            if action == "replace" and not old_text:
                return None, f"changes[{index}].old_text is required for replace."
            if action != "replace" and old_text is not None:
                return None, f"changes[{index}].old_text is only valid for replace."
        parsed.append(FileChange(path.strip(), action, content, expected_revision, old_text))
    return parsed, None


def _resolve_change_paths(
    base_path: object,
    changes: object,
) -> tuple[list[tuple[FileChange, Path]] | None, str | None]:
    """Validate changes and resolve their absolute target paths."""
    parsed, error = _parse_file_changes(changes)
    if parsed is None:
        return None, error
    if not isinstance(base_path, str) or not base_path.strip():
        return None, "base_path must be a nonempty directory path."
    try:
        base = Path(base_path).expanduser().resolve()
        resolved = [(change, (base / change.path).resolve()) for change in parsed]
    except (OSError, RuntimeError, ValueError) as path_error:
        return None, str(path_error)
    if len({path for _, path in resolved}) != len(resolved):
        return None, "changes must not target the same resolved file more than once."
    return resolved, None


def file_change_target_directories(arguments: object) -> list[Path] | dict | None:
    """Resolve directories that must be authorized for apply_file_changes."""
    targets = file_change_target_paths(arguments)
    if not isinstance(targets, list):
        return targets
    return list(dict.fromkeys(path.parent for path in targets))


def file_change_target_paths(arguments: object) -> list[Path] | dict | None:
    """Resolve exact file targets for approval and access decisions."""
    if not isinstance(arguments, dict):
        return None
    resolved, error = _resolve_change_paths(
        arguments.get("base_path"),
        arguments.get("changes"),
    )
    if resolved is None:
        return tool_error(
            error or "Could not resolve file-change targets.",
            status="access_validation_failed",
        )
    return [path for _, path in resolved]


def _file_revision(data: bytes) -> str:
    """Return a stable revision token for exact optimistic concurrency checks."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _decode_text_target(data: bytes, path: Path) -> tuple[str, str, bool] | dict:
    """Decode one bounded UTF-8 target and report its newline and BOM style."""
    if len(data) > MAX_FILE_BYTES:
        return tool_error(f"File exceeds the 1 MiB change limit: {path}")
    if b"\x00" in data:
        return tool_error(f"Binary file is not supported: {path}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return tool_error(f"File is not valid UTF-8 text: {path}")
    first_lf = text.find("\n")
    first_cr = text.find("\r")
    if first_cr >= 0 and (first_lf < 0 or first_cr < first_lf):
        newline = "\r\n" if first_cr + 1 < len(text) and text[first_cr + 1] == "\n" else "\r"
    else:
        newline = "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, newline, data.startswith(b"\xef\xbb\xbf")


def _normalize_text(text: str) -> str:
    """Normalize supported line endings for model-facing text operations."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _write_staged_file(path: Path, data: bytes) -> Path:
    """Write and flush replacement bytes beside the destination for atomic replace."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".harness-change-", dir=path.parent)
    staged = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def apply_file_changes(base_path: str, changes: list[dict]) -> dict:
    """Apply one preflighted batch of structured changes to authorized UTF-8 files.

    Args:
        base_path: Directory for relative change paths. Pass a get_location path
            for a named location, or ``.`` for the process working directory.
        changes: One to 100 objects with ``path``, ``action``, and ``content``.
            Actions are ``create``, ``overwrite``, ``append``, or ``replace``.
            Existing-file actions require ``expected_revision`` from read_file.
            ``replace`` also requires a nonempty ``old_text`` that occurs exactly
            once. Other actions must omit ``old_text``; create must omit
            ``expected_revision``. Paths may be absolute or relative to base_path.
            Example: ``[{"path": "app.py", "action": "append", "content":
            "print('done')", "expected_revision": "sha256:..."}]``.

    All changes are validated and staged before commit. Files must be UTF-8 text
    no larger than 1 MiB before and after modification. Append inserts one line
    separator when needed. Existing BOM and newline style are preserved.
    """
    resolved, resolve_error = _resolve_change_paths(base_path, changes)
    if resolved is None:
        return tool_error(resolve_error or "Could not resolve file changes.")
    base = Path(base_path).expanduser().resolve()
    if not base.is_dir():
        return tool_error(f"Base path is not a directory: {base}")

    prepared: list[tuple[FileChange, Path, bytes | None, bytes]] = []
    for change, target in resolved:
        if not target.parent.is_dir():
            return tool_error(f"Parent directory does not exist: {target.parent}")
        if target.exists() and not target.is_file():
            return tool_error(f"Change target is not a regular file: {target}")
        if change.action == "create":
            if target.exists():
                return tool_error(
                    f"Create target already exists: {target}.",
                    status="target_exists",
                    path=str(target),
                    required_action="read_then_choose_update_action",
                )
            updated_text = _normalize_text(change.content)
            original = None
            newline = "\n"
            has_bom = False
        else:
            if not target.is_file():
                return tool_error(f"Existing-file action target is not a regular file: {target}")
            try:
                original = target.read_bytes()
            except OSError as error:
                return tool_error(str(error))
            decoded = _decode_text_target(original, target)
            if isinstance(decoded, dict):
                return decoded
            current_revision = _file_revision(original)
            if change.expected_revision != current_revision:
                return tool_error(
                    f"File revision changed or does not match: {target}",
                    status="revision_mismatch",
                    path=str(target),
                    current_revision=current_revision,
                    required_action="reread_then_retry",
                )
            original_text, newline, has_bom = decoded
            content = _normalize_text(change.content)
            if change.action == "overwrite":
                updated_text = content
            elif change.action == "append":
                needs_separator = (
                    original_text
                    and content
                    and not original_text.endswith("\n")
                    and not content.startswith("\n")
                )
                separator = "\n" if needs_separator else ""
                updated_text = original_text + separator + content
            else:
                old_text = _normalize_text(change.old_text or "")
                occurrences = original_text.count(old_text)
                if occurrences != 1:
                    return tool_error(
                        f"replace old_text must occur exactly once in {target}; found {occurrences}.",
                        status="text_match_failed",
                        path=str(target),
                        occurrences=occurrences,
                        required_action="reread_then_retry",
                    )
                updated_text = original_text.replace(old_text, content, 1)
        encoded = updated_text.replace("\n", newline).encode("utf-8")
        if has_bom:
            encoded = b"\xef\xbb\xbf" + encoded
        if len(encoded) > MAX_FILE_BYTES:
            return tool_error(f"Result exceeds the 1 MiB change limit: {target}")
        prepared.append((change, target, original, encoded))

    staged_files: list[Path] = []
    applied: list[tuple[Path, bytes | None]] = []
    try:
        for _, target, original, encoded in prepared:
            staged = _write_staged_file(target, encoded)
            if original is not None:
                shutil.copymode(target, staged)
            staged_files.append(staged)
        for (change, target, original, _), staged in zip(prepared, staged_files, strict=True):
            if change.action == "create":
                if target.exists():
                    raise OSError(f"Create target appeared before commit: {target}")
                os.link(staged, target)
            else:
                if target.read_bytes() != original:
                    raise OSError(f"Change target changed before commit: {target}")
                os.replace(staged, target)
            applied.append((target, original))
    except OSError as error:
        rollback_errors = []
        for target, original in reversed(applied):
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    rollback_staged = _write_staged_file(target, original)
                    try:
                        shutil.copymode(target, rollback_staged)
                        os.replace(rollback_staged, target)
                    finally:
                        rollback_staged.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        detail = f"File changes failed and applied changes were rolled back: {error}"
        if rollback_errors:
            detail = f"File-change outcome is uncertain after rollback failure: {'; '.join(rollback_errors)}"
        return tool_error(detail, status="outcome_unknown" if rollback_errors else "rolled_back")
    finally:
        for staged in staged_files:
            staged.unlink(missing_ok=True)

    files = [
        {"path": str(target), "action": change.action, "revision": _file_revision(encoded)}
        for change, target, _, encoded in prepared
    ]
    return tool_success(
        affected_paths=[entry["path"] for entry in files],
        files=files,
        summary=f"Applied {len(files)} structured file change(s).",
        files_changed=len(prepared),
    )


def list_files(offset: int = 0, max_entries: int = 100, path: str = ".") -> dict:
    """List a bounded sorted page of immediate names in an authorized directory."""
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
    """Recursively find files by case-insensitive pattern in an authorized root.

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
    """Search nested UTF-8 files for literal text in an authorized path.

    Args:
        text: Nonempty, case-sensitive literal text to find within a source line.
        path: File or directory to search, absolute or relative to the working
            directory. Session authorization is enforced before this function.
        file_pattern: Optional case-insensitive filename wildcard, such as
            ``*.py``. It must be a filename pattern rather than a path.
        max_results: Maximum matching lines to return, between 1 and 100.

    Results include resolved paths, one-based line numbers, and line
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
        return tool_error("path must be a nonempty file or directory path.")
    if (not isinstance(file_pattern, str) or not file_pattern
            or any(character in file_pattern for character in "/\\")):
        return tool_error("file_pattern must be a nonempty filename wildcard, not a path.")
    if type(max_results) is not int or not 1 <= max_results <= 100:
        return tool_error("max_results must be an integer between 1 and 100.")

    try:
        workspace = Path.cwd().resolve()
        search_path = Path(path).expanduser().resolve()
        if not search_path.exists():
            return tool_error(f"Path does not exist: {search_path}")
        if not search_path.is_file() and not search_path.is_dir():
            return tool_error(f"Not a regular file or directory: {search_path}")
    except (OSError, ValueError) as error:
        return tool_error(str(error))

    result = tool_success(
        workspace=str(workspace),
        path=str(search_path),
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
            relative_parts = current.relative_to(search_path).parts
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
                    "path": str(current),
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
    """Read a raw page of an authorized UTF-8 text file with a revision token.

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

    content: list[str] = []
    size = 0
    for index in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
        source_line = lines[index]
        separator_size = 1 if content else 0
        if size + separator_size + len(source_line) > MAX_OUTPUT_CHARS:
            if not content:
                return tool_error(f"Line {index + 1} exceeds the output limit.")
            break
        content.append(source_line)
        size += separator_size + len(source_line)

    next_line = start_line + len(content)
    truncated = next_line <= len(lines)
    raw_content = "\n".join(content)
    if content and (truncated or text.endswith("\n")):
        raw_content += "\n"
    return tool_success(
        complete=not truncated,
        truncated=truncated,
        path=str(file_path),
        start_line=start_line,
        lines_read=len(content),
        total_lines=len(lines),
        content=raw_content,
        revision=_file_revision(data),
        next_line=next_line if truncated else None,
    )
