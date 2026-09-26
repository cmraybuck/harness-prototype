"""Append-only JSONL interaction logging with configurable redaction."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp suitable for persisted records."""
    return datetime.now(timezone.utc).isoformat()


class TurnLog:
    """Accumulate ordered events for one user turn before persistence."""

    def __init__(self, session_id: str, user_prompt: str) -> None:
        """Create stable identifiers and timing state for a new turn."""
        self.session_id = session_id
        self.turn_id = str(uuid4())
        self.started_at = utc_timestamp()
        self.started_monotonic = monotonic()
        self.user_prompt = user_prompt
        self.events: list[dict[str, Any]] = []

    def add_event(self, event_type: str, **fields: Any) -> None:
        """Append a timestamped event while preserving observation order."""
        self.events.append({
            "sequence": len(self.events) + 1,
            "timestamp": utc_timestamp(),
            "type": event_type,
            **deepcopy(fields),
        })

    def record(self, outcome: str, error: str | None = None) -> dict[str, Any]:
        """Build the complete versioned JSON object for this turn."""
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "started_at": self.started_at,
            "finished_at": utc_timestamp(),
            "duration_ms": round((monotonic() - self.started_monotonic) * 1000, 3),
            "outcome": outcome,
            "error": error,
            "user_prompt": self.user_prompt,
            "events": self.events,
        }


class InteractionLogger:
    """Persist one redacted JSON object per turn without affecting the chat."""

    def __init__(self, directory: Path, redact_values: tuple[str, ...] = ()) -> None:
        """Reserve a unique session log path without replacing an existing file."""
        self.session_id = str(uuid4())
        self.redact_values = tuple(value for value in redact_values if value)
        self.path: Path | None = None
        self.error: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            path = directory / f"session-{stamp}-{self.session_id}.jsonl"
            path.open("x", encoding="utf-8").close()
            self.path = path
        except OSError as error:
            self.error = str(error)

    def start_turn(self, user_prompt: str) -> TurnLog:
        """Return an in-memory recorder even when persistence is unavailable."""
        return TurnLog(self.session_id, user_prompt)

    def finish_turn(self, turn: TurnLog, outcome: str, error: str | None = None) -> None:
        """Append a redacted turn record, disabling logging after a write failure."""
        if self.path is None:
            return
        try:
            record = self._redact(turn.record(outcome, error))
            with self.path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError) as log_error:
            self.error = str(log_error)
            self.path = None

    def _redact(self, value: Any) -> Any:
        """Replace configured secret substrings recursively in keys and values."""
        if isinstance(value, str):
            for secret in self.redact_values:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(str(key)): self._redact(item) for key, item in value.items()}
        return value
