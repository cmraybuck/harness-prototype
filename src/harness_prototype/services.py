"""Lifecycle management for configured background services."""

import os
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import BinaryIO
from uuid import uuid4

from .config import SERVICES, TEST_OUTPUT_LIMIT, ServiceConfig
from .execution import _BoundedOutput, _terminate_process
from .tool_results import tool_error, tool_success

READINESS_POLL_SECONDS = 0.02
NO_READINESS_GRACE_SECONDS = 0.1


class _ManagedService:
    """Retain one harness-launched process and its bounded output."""

    def __init__(
        self, name: str, handle: str, process: subprocess.Popen[bytes], config: ServiceConfig,
    ) -> None:
        """Initialize process metadata and synchronized output captures."""
        self.name = name
        self.handle = handle
        self.process = process
        self.config = config
        self.started = monotonic()
        self.stdout = _BoundedOutput(TEST_OUTPUT_LIMIT)
        self.stderr = _BoundedOutput(TEST_OUTPUT_LIMIT)
        self.output_lock = Lock()
        self.ready = Event()
        self.threads = [
            Thread(target=self._drain, args=(process.stdout, self.stdout), daemon=True),
            Thread(target=self._drain, args=(process.stderr, self.stderr), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _drain(self, stream: BinaryIO, capture: _BoundedOutput) -> None:
        """Drain one service pipe while protecting snapshots from concurrent writes."""
        ready_bytes = (
            self.config.ready_text.encode("utf-8")
            if self.config.ready_text is not None else None
        )
        overlap = b""
        try:
            while chunk := os.read(stream.fileno(), 1024):
                if ready_bytes is not None:
                    candidate = overlap + chunk
                    if ready_bytes in candidate:
                        self.ready.set()
                    overlap_length = len(ready_bytes) - 1
                    overlap = candidate[-overlap_length:] if overlap_length else b""
                with self.output_lock:
                    capture.add(chunk)
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def output(self) -> dict:
        """Return a consistent bounded snapshot of both output streams."""
        with self.output_lock:
            stdout, stdout_truncated = self.stdout.text()
            stderr, stderr_truncated = self.stderr.text()
        return {
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "output_truncated": stdout_truncated or stderr_truncated,
        }

    def join_output(self) -> None:
        """Wait for output drainers after the process has exited."""
        for thread in self.threads:
            thread.join(timeout=5)


_services_by_handle: dict[str, _ManagedService] = {}
_handles_by_name: dict[str, str] = {}
_registry_lock = Lock()


def _service_directory(config: ServiceConfig, workspace: Path) -> Path | dict:
    """Resolve a configured working directory without allowing workspace escape."""
    try:
        directory = (workspace / config.cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return tool_error("The configured service directory is invalid.", status="invalid_service")
    if directory != workspace and not directory.is_relative_to(workspace):
        return tool_error(
            "The configured service directory must stay inside the active workspace.",
            status="invalid_service",
        )
    if not directory.is_dir():
        return tool_error(
            "The configured service directory does not exist.", status="invalid_service"
        )
    return directory


def _service_payload(service: _ManagedService, status: str) -> dict:
    """Build a service result containing identity, state, and bounded logs."""
    output = service.output()
    truncated = output["output_truncated"]
    return tool_success(
        complete=not truncated,
        truncated=truncated,
        status=status,
        service=service.name,
        handle=service.handle,
        pid=service.process.pid,
        exit_code=service.process.poll(),
        duration_ms=round((monotonic() - service.started) * 1000, 3),
        **output,
    )


def start_service(name: str) -> dict:
    """Start one exactly configured background service and wait for readiness.

    Args:
        name: Exact service name from ``HARNESS_SERVICES``.

    Duplicate starts return the existing handle and current logs without launching
    another process. A configured readiness string may appear on either output
    stream; without one, the service is ready after a short survival check.
    """
    if not isinstance(name, str) or name not in SERVICES:
        return tool_error("Unknown configured service name.", status="unknown_service")
    with _registry_lock:
        existing_handle = _handles_by_name.get(name)
        existing = _services_by_handle.get(existing_handle or "")
        if existing is not None and existing.process.poll() is None:
            return _service_payload(existing, "already_running")
        if existing is not None:
            existing.join_output()
            _services_by_handle.pop(existing.handle, None)
            _handles_by_name.pop(name, None)

        config = SERVICES[name]
        workspace = Path.cwd().resolve()
        directory = _service_directory(config, workspace)
        if isinstance(directory, dict):
            return directory
        try:
            process = subprocess.Popen(
                list(config.command),
                cwd=directory,
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
            return tool_error(
                f"Could not launch configured service '{name}': {error}",
                status="launch_error",
            )
        service = _ManagedService(name, uuid4().hex, process, config)
        _services_by_handle[service.handle] = service
        _handles_by_name[name] = service.handle

    deadline = monotonic() + config.startup_timeout_seconds
    while monotonic() < deadline:
        exit_code = process.poll()
        output = service.output()
        if exit_code is not None:
            service.join_output()
            with _registry_lock:
                _services_by_handle.pop(service.handle, None)
                _handles_by_name.pop(name, None)
            return tool_error(
                f"Configured service '{name}' exited before becoming ready.",
                status="startup_failed",
                service=name,
                handle=service.handle,
                exit_code=exit_code,
                **service.output(),
            )
        if config.ready_text is not None:
            if service.ready.is_set():
                return _service_payload(service, "ready")
        elif monotonic() - service.started >= NO_READINESS_GRACE_SECONDS:
            return _service_payload(service, "ready")
        sleep(READINESS_POLL_SECONDS)

    _terminate_process(process)
    service.join_output()
    with _registry_lock:
        _services_by_handle.pop(service.handle, None)
        _handles_by_name.pop(name, None)
    return tool_error(
        f"Configured service '{name}' did not become ready within "
        f"{config.startup_timeout_seconds} seconds.",
        status="readiness_timeout",
        service=name,
        handle=service.handle,
        exit_code=process.returncode,
        **service.output(),
    )


def stop_service(handle: str) -> dict:
    """Stop one process previously launched by ``start_service``.

    Args:
        handle: Exact opaque handle returned by ``start_service``.

    Unknown handles are rejected and can never identify arbitrary operating-system
    processes. The result includes the service's final bounded logs and exit state.
    """
    if not isinstance(handle, str) or not handle:
        return tool_error("handle must be a nonempty string.", status="unknown_handle")
    with _registry_lock:
        service = _services_by_handle.pop(handle, None)
        if service is None:
            return tool_error("Unknown managed service handle.", status="unknown_handle")
        _handles_by_name.pop(service.name, None)
    was_running = service.process.poll() is None
    if was_running:
        _terminate_process(service.process)
    service.join_output()
    return _service_payload(service, "stopped" if was_running else "exited")


def stop_all_services() -> None:
    """Stop and forget every process launched by this harness session."""
    with _registry_lock:
        handles = list(_services_by_handle)
    for handle in handles:
        stop_service(handle)
