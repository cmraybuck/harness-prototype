"""Constrained test execution with bounded output and hard time limits."""

import os
from pathlib import Path
import signal
import subprocess
from threading import Thread
from time import monotonic
from typing import BinaryIO

from .config import (
    TARGETED_TEST_COMMAND,
    TEST_COMMAND,
    TEST_OUTPUT_LIMIT,
    TEST_TIMEOUT_SECONDS,
)
from .tool_results import tool_error, tool_success

MAX_TEST_TARGETS = 50
MAX_TEST_TARGET_LENGTH = 500
_TRUNCATION_MARKER = b"\n... output truncated ...\n"


class _BoundedOutput:
    """Retain a fixed-size head and tail while draining an entire output stream."""

    def __init__(self, limit: int) -> None:
        """Allocate fixed bounds for one captured byte stream."""
        self.limit = limit
        content_limit = max(0, limit - len(_TRUNCATION_MARKER))
        self.head_limit = content_limit // 2
        self.tail_limit = content_limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        """Consume bytes without allowing retained memory to exceed the limit."""
        self.total += len(chunk)
        head_remaining = self.head_limit - len(self.head)
        if head_remaining > 0:
            self.head.extend(chunk[:head_remaining])
            chunk = chunk[head_remaining:]
        if self.tail_limit and chunk:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def text(self) -> tuple[str, bool]:
        """Decode the retained bytes and report whether any output was omitted."""
        truncated = self.total > self.limit
        if not truncated:
            content = bytes(self.head + self.tail)
        elif self.limit <= len(_TRUNCATION_MARKER):
            content = bytes((self.head + self.tail)[:self.limit])
        else:
            content = bytes(self.head) + _TRUNCATION_MARKER + bytes(self.tail)
        return content.decode("utf-8", errors="replace"), truncated


def _validate_targets(targets: list[str] | None, workspace: Path) -> dict | None:
    """Reject malformed selectors and path-like targets that leave the workspace."""
    if targets is None:
        return None
    if not isinstance(targets, list) or not targets:
        return tool_error("targets must be a nonempty array when provided.", status="invalid_target")
    if len(targets) > MAX_TEST_TARGETS:
        return tool_error(
            f"targets accepts at most {MAX_TEST_TARGETS} entries.",
            status="invalid_target",
        )
    for target in targets:
        if (
            not isinstance(target, str)
            or not target.strip()
            or len(target) > MAX_TEST_TARGET_LENGTH
            or any(not character.isprintable() for character in target)
        ):
            return tool_error(
                "Every target must be a nonempty printable string no longer than "
                f"{MAX_TEST_TARGET_LENGTH} characters.",
                status="invalid_target",
            )
        if target.startswith("-"):
            return tool_error(
                "Test targets cannot be command-line options.",
                status="invalid_target",
            )
        path_part = target.split("::", 1)[0]
        if "/" in path_part or "\\" in path_part or Path(path_part).is_absolute():
            try:
                resolved = (workspace / path_part).resolve()
            except (OSError, RuntimeError, ValueError):
                return tool_error("A test target path is invalid.", status="invalid_target")
            if resolved != workspace and not resolved.is_relative_to(workspace):
                return tool_error(
                    "Test target paths must stay inside the active workspace.",
                    status="invalid_target",
                )
    return None


def _build_test_command(targets: list[str] | None) -> list[str]:
    """Select the configured command and expand targets as separate arguments."""
    if targets is None:
        return list(TEST_COMMAND)
    command: list[str] = []
    for argument in TARGETED_TEST_COMMAND:
        command.extend(targets if argument == "{targets}" else [argument])
    return command


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the launched process group so timed-out child processes do not survive."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _drain_output(stream: BinaryIO, capture: _BoundedOutput) -> None:
    """Drain a child pipe concurrently so verbose tests cannot deadlock."""
    try:
        while chunk := stream.read(8192):
            capture.add(chunk)
    except (OSError, ValueError):
        # A concurrent shutdown may close a pipe while its drain thread is reading.
        pass
    finally:
        stream.close()


def run_tests(targets: list[str] | None = None) -> dict:
    """Run the configured test suite or selected targets in the active workspace.

    Omit targets for the configured full suite. When provided, targets are inserted
    at the configured ``{targets}`` argument as separate values; options and paths
    outside the workspace are rejected. The command never uses a shell. Output,
    runtime, and target counts are bounded. Results distinguish passed tests,
    test failures, timeouts, invalid targets, and process launch errors.
    """
    workspace = Path.cwd().resolve()
    invalid = _validate_targets(targets, workspace)
    if invalid is not None:
        return invalid
    command = _build_test_command(targets)
    started = monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
    except OSError as error:
        duration_ms = round((monotonic() - started) * 1000, 3)
        return tool_error(
            f"Could not launch the configured test command: {error}",
            status="launch_error",
            stdout="",
            stderr="",
            exit_code=None,
            duration_ms=duration_ms,
            stdout_truncated=False,
            stderr_truncated=False,
            output_truncated=False,
        )

    stdout_capture = _BoundedOutput(TEST_OUTPUT_LIMIT)
    stderr_capture = _BoundedOutput(TEST_OUTPUT_LIMIT)
    output_threads = [
        Thread(target=_drain_output, args=(process.stdout, stdout_capture), daemon=True),
        Thread(target=_drain_output, args=(process.stderr, stderr_capture), daemon=True),
    ]
    for thread in output_threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process)
    except (Exception, KeyboardInterrupt):
        _terminate_process(process)
        raise
    finally:
        for thread in output_threads:
            thread.join()

    duration_ms = round((monotonic() - started) * 1000, 3)
    stdout, stdout_truncated = stdout_capture.text()
    stderr, stderr_truncated = stderr_capture.text()
    output_truncated = stdout_truncated or stderr_truncated
    payload = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": process.returncode,
        "duration_ms": duration_ms,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_truncated": output_truncated,
    }
    if timed_out:
        return tool_error(
            f"Test execution exceeded the {TEST_TIMEOUT_SECONDS}-second timeout.",
            status="timed_out",
            **payload,
        )
    status = "passed" if process.returncode == 0 else "failed"
    return tool_success(
        complete=not output_truncated,
        truncated=output_truncated,
        status=status,
        **payload,
    )
