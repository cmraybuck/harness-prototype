"""Transactional, versioned SQLite storage for bounded local application state."""

import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 4
MAX_VALUE_BYTES = 64 * 1024
MAX_SCOPE_LENGTH = 100
MAX_KEY_LENGTH = 200


class StateStoreError(RuntimeError):
    """Report a predictable application-state storage failure."""


class UnsupportedSchemaError(StateStoreError):
    """Report a database created by a newer unsupported application version."""


class CorruptStateStoreError(StateStoreError):
    """Report a database that SQLite cannot read safely."""


class StateTransaction:
    """Apply related state mutations through one caller-controlled transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Bind mutations to one active SQLite transaction."""
        self._connection = connection

    def set(self, scope: str, key: str, value: Any) -> None:
        """Store one bounded JSON value under a portable scope and key."""
        normalized_scope = _validate_identifier(scope, "scope", MAX_SCOPE_LENGTH)
        normalized_key = _validate_identifier(key, "key", MAX_KEY_LENGTH)
        encoded = _encode_value(value)
        self._connection.execute(
            """
            INSERT INTO state_entries(scope, key, value_json, updated_at_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope, key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at_utc = excluded.updated_at_utc
            """,
            (normalized_scope, normalized_key, encoded, _utc_timestamp()),
        )

    def get(self, scope: str, key: str, default: Any = None) -> Any:
        """Return one decoded value within the active transaction."""
        normalized_scope = _validate_identifier(scope, "scope", MAX_SCOPE_LENGTH)
        normalized_key = _validate_identifier(key, "key", MAX_KEY_LENGTH)
        row = self._connection.execute(
            "SELECT value_json FROM state_entries WHERE scope = ? AND key = ?",
            (normalized_scope, normalized_key),
        ).fetchone()
        return default if row is None else json.loads(row[0])

    def delete(self, scope: str, key: str) -> bool:
        """Delete one state value and report whether it existed."""
        normalized_scope = _validate_identifier(scope, "scope", MAX_SCOPE_LENGTH)
        normalized_key = _validate_identifier(key, "key", MAX_KEY_LENGTH)
        cursor = self._connection.execute(
            "DELETE FROM state_entries WHERE scope = ? AND key = ?",
            (normalized_scope, normalized_key),
        )
        return cursor.rowcount > 0


class StateStore:
    """Own schema migration and short-lived connections for one SQLite database."""

    def __init__(self, path: Path, *, timeout_seconds: float = 5.0) -> None:
        """Initialize or migrate a store at an ordinary filesystem path."""
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path.")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self.path = path.expanduser().resolve()
        self.timeout_seconds = timeout_seconds
        self._migration_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, scope: str, key: str, default: Any = None) -> Any:
        """Return one decoded JSON value, or the supplied default when absent."""
        normalized_scope = _validate_identifier(scope, "scope", MAX_SCOPE_LENGTH)
        normalized_key = _validate_identifier(key, "key", MAX_KEY_LENGTH)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json FROM state_entries WHERE scope = ? AND key = ?",
                (normalized_scope, normalized_key),
            ).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, scope: str, key: str, value: Any) -> None:
        """Atomically store one bounded JSON value."""
        with self.transaction() as transaction:
            transaction.set(scope, key, value)

    def delete(self, scope: str, key: str) -> bool:
        """Atomically delete one value and report whether it existed."""
        with self.transaction() as transaction:
            return transaction.delete(scope, key)

    @contextmanager
    def transaction(self) -> Iterator[StateTransaction]:
        """Commit all yielded mutations together, or roll them all back on failure."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield StateTransaction(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent SQLite backup and atomically replace the destination."""
        destination = _validate_distinct_path(destination, self.path, "backup destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_path(destination)
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                target.commit()
            _validate_database(temporary, allow_older=True)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def restore_from(self, source: Path) -> None:
        """Validate and atomically restore a compatible backup over this store."""
        source = _validate_distinct_path(source, self.path, "restore source")
        if not source.is_file():
            raise StateStoreError(f"Restore source does not exist: {source}")
        _validate_database(source, allow_older=True)
        temporary = _temporary_path(self.path)
        try:
            with closing(_readonly_connection(source)) as backup, closing(
                sqlite3.connect(temporary)
            ) as target:
                backup.backup(target)
                target.commit()
            os.replace(temporary, self.path)
            _remove_sidecars(self.path)
            self._initialize()
        finally:
            temporary.unlink(missing_ok=True)

    def reset(self) -> None:
        """Delete all application state while retaining an initialized current schema."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM durable_grant_events")
            connection.execute("DELETE FROM durable_grants")
            connection.execute("DELETE FROM conversations")
            connection.execute("DELETE FROM state_entries")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """Open one concurrency-safe connection with explicit durability settings."""
        connection = self._raw_connect()
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.DatabaseError as error:
            connection.close()
            raise CorruptStateStoreError(f"Unable to open state database: {error}") from error

    def _raw_connect(self) -> sqlite3.Connection:
        """Open a connection without changing database settings or schema metadata."""
        try:
            return sqlite3.connect(
                self.path,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as error:
            raise CorruptStateStoreError(f"Unable to open state database: {error}") from error

    def _initialize(self) -> None:
        """Check integrity and apply forward-only migrations in one exclusive transaction."""
        with self._migration_lock:
            connection = self._raw_connect()
            try:
                connection.execute(
                    f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
                )
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise CorruptStateStoreError(f"State database integrity check failed: {result[0]}")
                connection.execute("BEGIN EXCLUSIVE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > CURRENT_SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        f"State schema {version} is newer than supported schema "
                        f"{CURRENT_SCHEMA_VERSION}."
                    )
                for target_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
                    _apply_migration(connection, target_version)
                    connection.execute(f"PRAGMA user_version = {target_version}")
                connection.commit()
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
            except (UnsupportedSchemaError, CorruptStateStoreError):
                connection.rollback()
                raise
            except sqlite3.DatabaseError as error:
                connection.rollback()
                raise CorruptStateStoreError(
                    f"Unable to initialize state database: {error}"
                ) from error
            finally:
                connection.close()


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    """Apply one known forward schema migration."""
    if version == 1:
        connection.execute(
            """
            CREATE TABLE state_entries (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL CHECK(json_valid(value_json)),
                PRIMARY KEY(scope, key)
            ) WITHOUT ROWID
            """
        )
    elif version == 2:
        connection.execute(
            "ALTER TABLE state_entries ADD COLUMN updated_at_utc TEXT NOT NULL DEFAULT ''"
        )
    elif version == 3:
        for statement in (
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                workspace TEXT NOT NULL,
                capability_groups_json TEXT NOT NULL DEFAULT '[]'
                    CHECK(json_valid(capability_groups_json)),
                active_skills_json TEXT NOT NULL DEFAULT '[]'
                    CHECK(json_valid(active_skills_json)),
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE conversation_messages (
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK(sequence >= 1),
                message_json TEXT NOT NULL CHECK(json_valid(message_json)),
                PRIMARY KEY(conversation_id, sequence)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE conversation_turns (
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,
                turn_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence >= 1),
                status TEXT NOT NULL,
                user_prompt TEXT NOT NULL,
                error TEXT,
                events_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(events_json)),
                retained_messages_json TEXT NOT NULL DEFAULT '[]'
                    CHECK(json_valid(retained_messages_json)),
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                PRIMARY KEY(conversation_id, turn_id),
                UNIQUE(conversation_id, sequence)
            )
            """,
            """
            CREATE INDEX conversations_updated_order
                ON conversations(updated_at_utc DESC, conversation_id DESC)
            """,
        ):
            connection.execute(statement)
    elif version == 4:
        for statement in (
            """
            CREATE TABLE durable_grants (
                grant_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                workspace TEXT NOT NULL,
                target TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                last_used_at_utc TEXT NOT NULL,
                UNIQUE(kind, workspace, target)
            )
            """,
            """
            CREATE TABLE durable_grant_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX durable_grants_created_order
                ON durable_grants(created_at_utc, grant_id)
            """,
        ):
            connection.execute(statement)
    else:
        raise StateStoreError(f"No migration is defined for schema {version}.")


def _validate_identifier(value: str, name: str, maximum: int) -> str:
    """Validate a portable bounded state scope or key."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{name} must be a nonempty trimmed printable string up to {maximum} characters.")
    return value


def _encode_value(value: Any) -> str:
    """Encode one JSON value and enforce its stored UTF-8 size bound."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be valid JSON data.") from error
    if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(f"encoded value must not exceed {MAX_VALUE_BYTES} bytes.")
    return encoded


def _utc_timestamp() -> str:
    """Return a portable, timezone-qualified update timestamp."""
    return datetime.now(UTC).isoformat()


def _temporary_path(destination: Path) -> Path:
    """Reserve and return a temporary sibling path for atomic replacement."""
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(raw_path)


def _validate_distinct_path(path: Path, database: Path, name: str) -> Path:
    """Resolve a backup path and reject the live database itself."""
    if not isinstance(path, Path):
        raise TypeError(f"{name} must be a pathlib.Path.")
    resolved = path.expanduser().resolve()
    if resolved == database:
        raise ValueError(f"{name} must differ from the live database path.")
    return resolved


def _validate_database(path: Path, *, allow_older: bool) -> None:
    """Reject corrupt, empty, or incompatible backup databases without changing them."""
    try:
        with closing(
            _readonly_connection(path)
        ) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state_entries'"
            ).fetchone()
            conversations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversations'"
            ).fetchone()
            durable_grants = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'durable_grants'"
            ).fetchone()
    except sqlite3.DatabaseError as error:
        raise CorruptStateStoreError(f"Invalid state database: {error}") from error
    if result != ("ok",) or table is None or version < (1 if allow_older else CURRENT_SCHEMA_VERSION):
        raise CorruptStateStoreError("Invalid or incomplete state database.")
    if version >= 3 and conversations is None:
        raise CorruptStateStoreError("Invalid or incomplete state database.")
    if version >= 4 and durable_grants is None:
        raise CorruptStateStoreError("Invalid or incomplete state database.")
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"State schema {version} is newer than supported schema {CURRENT_SCHEMA_VERSION}."
        )


def _readonly_connection(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without permitting source modification."""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _remove_sidecars(path: Path) -> None:
    """Remove stale SQLite sidecars after atomically replacing a closed database."""
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class DurableGrantStore:
    """Persist and audit narrowly scoped local permission grants."""

    def __init__(self, state: StateStore) -> None:
        """Bind durable grant operations to the shared application database."""
        self.state = state

    def create(self, grant_id: str, kind: str, workspace: str, target: str) -> dict[str, Any]:
        """Create or refresh one exact grant without widening its scope."""
        self._validate(grant_id, kind, workspace, target)
        timestamp = _utc_timestamp()
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT grant_id, created_at_utc FROM durable_grants "
                    "WHERE kind = ? AND workspace = ? AND target = ?",
                    (kind, workspace, target),
                ).fetchone()
                effective_id = existing[0] if existing else grant_id
                created_at = existing[1] if existing else timestamp
                if existing:
                    connection.execute(
                        "UPDATE durable_grants SET last_used_at_utc = ? WHERE grant_id = ?",
                        (timestamp, effective_id),
                    )
                else:
                    connection.execute(
                        "INSERT INTO durable_grants VALUES (?, ?, ?, ?, ?, ?)",
                        (effective_id, kind, workspace, target, timestamp, timestamp),
                    )
                self._audit(connection, effective_id, "created", kind, timestamp)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "grant_id": effective_id, "kind": kind, "workspace": workspace,
            "target": target, "created_at": created_at, "last_used_at": timestamp,
        }

    def match(self, kind: str, workspace: str, target: str) -> dict[str, Any] | None:
        """Reuse only an exact valid grant and record its last use."""
        self._validate("lookup", kind, workspace, target)
        timestamp = _utc_timestamp()
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT grant_id, created_at_utc FROM durable_grants "
                "WHERE kind = ? AND workspace = ? AND target = ?",
                (kind, workspace, target),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE durable_grants SET last_used_at_utc = ? WHERE grant_id = ?",
                (timestamp, row[0]),
            )
            self._audit(connection, row[0], "reused", kind, timestamp)
            connection.commit()
        return {
            "grant_id": row[0], "kind": kind, "workspace": workspace,
            "target": target, "created_at": row[1], "last_used_at": timestamp,
        }

    def list(self) -> list[dict[str, Any]]:
        """List durable grants in stable creation order."""
        with closing(self.state._connect()) as connection:
            rows = connection.execute(
                "SELECT grant_id, kind, workspace, target, created_at_utc, last_used_at_utc "
                "FROM durable_grants ORDER BY created_at_utc, grant_id"
            ).fetchall()
        return [
            {"grant_id": row[0], "kind": row[1], "workspace": row[2],
             "target": row[3], "created_at": row[4], "last_used_at": row[5]}
            for row in rows
        ]

    def revoke(self, grant_id: str) -> bool:
        """Revoke one grant immediately while retaining a payload-free audit event."""
        _validate_identifier(grant_id, "grant_id", MAX_KEY_LENGTH)
        timestamp = _utc_timestamp()
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind FROM durable_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            connection.execute("DELETE FROM durable_grants WHERE grant_id = ?", (grant_id,))
            self._audit(connection, grant_id, "revoked", row[0], timestamp)
            connection.commit()
        return True

    def record_denial(self, kind: str) -> None:
        """Audit a denied durable-capable request without retaining its payload."""
        if kind not in {
            "directory", "executable", "file", "operation", "service", "risk_category", "tool"
        }:
            raise ValueError("kind is not a supported durable grant kind.")
        with closing(self.state._connect()) as connection:
            self._audit(connection, "none", "denied", kind, _utc_timestamp())

    @staticmethod
    def _validate(grant_id: str, kind: str, workspace: str, target: str) -> None:
        """Validate bounded grant fields at the persistence boundary."""
        _validate_identifier(grant_id, "grant_id", MAX_KEY_LENGTH)
        if kind not in {
            "directory", "executable", "file", "operation", "service", "risk_category"
        }:
            raise ValueError("kind is not a supported durable grant kind.")
        for name, value in (("workspace", workspace), ("target", target)):
            if (
                not isinstance(value, str) or not value or value != value.strip()
                or len(value) > 4096 or any(not character.isprintable() for character in value)
            ):
                raise ValueError(f"{name} must be a nonempty printable string up to 4096 characters.")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, grant_id: str, action: str, kind: str, timestamp: str,
    ) -> None:
        """Append a grant lifecycle event without paths, commands, or arguments."""
        connection.execute(
            "INSERT INTO durable_grant_events(grant_id, action, kind, created_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (grant_id, action, kind, timestamp),
        )


class ConversationStore:
    """Persist resumable conversations in the shared versioned state database."""

    def __init__(self, state: StateStore) -> None:
        """Bind conversation operations to an initialized state store."""
        self.state = state
        with closing(self.state._connect()) as connection:
            connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'interrupted',
                    error = 'Backend stopped before the turn completed.',
                    finished_at_utc = ?
                WHERE status = 'running'
                """,
                (_utc_timestamp(),),
            )

    def create(
        self,
        conversation_id: str,
        title: str,
        model: str,
        workspace: Path,
        messages: list[dict[str, Any]],
    ) -> None:
        """Create one conversation and its initial ordered model history."""
        timestamp = _utc_timestamp()
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, title, model, workspace, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (conversation_id, title, model, str(workspace), timestamp, timestamp),
                )
                self._replace_messages(connection, conversation_id, messages)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def load(self, conversation_id: str) -> dict[str, Any] | None:
        """Load one conversation with model-facing messages in exact sequence."""
        with closing(self.state._connect()) as connection:
            row = connection.execute(
                """
                SELECT conversation_id, title, model, workspace,
                       capability_groups_json, active_skills_json,
                       created_at_utc, updated_at_utc
                FROM conversations WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            messages = [
                json.loads(item[0])
                for item in connection.execute(
                    """
                    SELECT message_json FROM conversation_messages
                    WHERE conversation_id = ? ORDER BY sequence
                    """,
                    (conversation_id,),
                )
            ]
        return {
            "conversation_id": row[0], "title": row[1], "model": row[2],
            "workspace": row[3], "capability_groups": json.loads(row[4]),
            "active_skills": json.loads(row[5]), "created_at": row[6],
            "updated_at": row[7], "messages": messages,
        }

    def list_conversations(
        self, limit: int, after: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List conversations by immutable creation order using an ID cursor."""
        with closing(self.state._connect()) as connection:
            cursor = None
            if after is not None:
                cursor = connection.execute(
                    "SELECT created_at_utc, conversation_id FROM conversations WHERE conversation_id = ?",
                    (after,),
                ).fetchone()
                if cursor is None:
                    raise ValueError("Conversation cursor was not found.")
            where = ""
            arguments: list[Any] = []
            if cursor:
                where = "WHERE (created_at_utc, conversation_id) < (?, ?)"
                arguments.extend(cursor)
            arguments.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT conversation_id, title, model, workspace, created_at_utc, updated_at_utc
                FROM conversations {where}
                ORDER BY created_at_utc DESC, conversation_id DESC LIMIT ?
                """,
                arguments,
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {"conversation_id": row[0], "title": row[1], "model": row[2],
             "workspace": row[3], "created_at": row[4], "updated_at": row[5]}
            for row in rows
        ]
        return items, items[-1]["conversation_id"] if has_more else None

    def update_metadata(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        workspace: Path | None = None,
    ) -> None:
        """Update selected persisted metadata without changing unrelated fields."""
        fields = ["updated_at_utc = ?"]
        values: list[Any] = [_utc_timestamp()]
        for column, value in (("title", title), ("model", model), ("workspace", workspace)):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(str(value))
        values.append(conversation_id)
        with closing(self.state._connect()) as connection:
            connection.execute(
                f"UPDATE conversations SET {', '.join(fields)} WHERE conversation_id = ?",
                values,
            )

    def save_runtime(self, runtime: Any) -> None:
        """Atomically checkpoint ordered messages and conversation-scoped selections."""
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE conversations SET model = ?, workspace = ?,
                        capability_groups_json = ?, active_skills_json = ?, updated_at_utc = ?
                    WHERE conversation_id = ?
                    """,
                    (runtime.model, str(runtime.workspace),
                     json.dumps(sorted(runtime.capability_state.active_groups)),
                     json.dumps(list(runtime.skill_state.active_ids)), _utc_timestamp(),
                     runtime.conversation_id),
                )
                self._replace_messages(connection, runtime.conversation_id, runtime.messages)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def start_turn(self, conversation_id: str, turn_id: str, user_prompt: str) -> None:
        """Record an active turn before model work begins."""
        with closing(self.state._connect()) as connection:
            next_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    conversation_id, turn_id, sequence, status, user_prompt, started_at_utc
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (conversation_id, turn_id, next_sequence, user_prompt, _utc_timestamp()),
            )

    def save_turn_event(self, conversation_id: str, turn_id: str, event: dict[str, Any]) -> None:
        """Append one ordered runtime event to an active persisted turn."""
        with closing(self.state._connect()) as connection:
            row = connection.execute(
                "SELECT events_json FROM conversation_turns WHERE conversation_id = ? AND turn_id = ?",
                (conversation_id, turn_id),
            ).fetchone()
            events = json.loads(row[0]) if row else []
            events.append(event)
            connection.execute(
                "UPDATE conversation_turns SET events_json = ? WHERE conversation_id = ? AND turn_id = ?",
                (json.dumps(events, ensure_ascii=False, separators=(",", ":")), conversation_id, turn_id),
            )

    def checkpoint_turn(
        self, runtime: Any, turn_id: str, event: dict[str, Any],
    ) -> None:
        """Atomically retain a completed tool event and its exact model history."""
        with closing(self.state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT events_json FROM conversation_turns WHERE conversation_id = ? AND turn_id = ?",
                    (runtime.conversation_id, turn_id),
                ).fetchone()
                events = json.loads(row[0]) if row else []
                events.append(event)
                encoded_messages = json.dumps(
                    runtime.messages, ensure_ascii=False, separators=(",", ":")
                )
                connection.execute(
                    """
                    UPDATE conversation_turns SET events_json = ?, retained_messages_json = ?
                    WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (json.dumps(events, ensure_ascii=False, separators=(",", ":")),
                     encoded_messages, runtime.conversation_id, turn_id),
                )
                connection.execute(
                    """
                    UPDATE conversations SET model = ?, workspace = ?,
                        capability_groups_json = ?, active_skills_json = ?, updated_at_utc = ?
                    WHERE conversation_id = ?
                    """,
                    (runtime.model, str(runtime.workspace),
                     json.dumps(sorted(runtime.capability_state.active_groups)),
                     json.dumps(list(runtime.skill_state.active_ids)), _utc_timestamp(),
                     runtime.conversation_id),
                )
                self._replace_messages(connection, runtime.conversation_id, runtime.messages)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_turn_messages(
        self, conversation_id: str, turn_id: str, messages: list[dict[str, Any]],
    ) -> None:
        """Checkpoint the exact retained model history associated with a turn."""
        encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        with closing(self.state._connect()) as connection:
            connection.execute(
                """
                UPDATE conversation_turns SET retained_messages_json = ?
                WHERE conversation_id = ? AND turn_id = ?
                """,
                (encoded, conversation_id, turn_id),
            )

    def finish_turn(
        self, conversation_id: str, turn_id: str, status: str, error: str | None,
    ) -> None:
        """Finalize one turn with its explicit terminal status."""
        with closing(self.state._connect()) as connection:
            connection.execute(
                """
                UPDATE conversation_turns SET status = ?, error = ?, finished_at_utc = ?
                WHERE conversation_id = ? AND turn_id = ?
                """,
                (status, error, _utc_timestamp(), conversation_id, turn_id),
            )

    def history(
        self, conversation_id: str, limit: int, after: int = 0,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Return append-stable turn history after a monotonically increasing cursor."""
        with closing(self.state._connect()) as connection:
            rows = connection.execute(
                """
                SELECT turn_id, sequence, status, user_prompt, error, events_json,
                       retained_messages_json, started_at_utc, finished_at_utc
                FROM conversation_turns
                WHERE conversation_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (conversation_id, after, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {"turn_id": row[0], "sequence": row[1], "status": row[2],
             "user_prompt": row[3], "error": row[4], "events": json.loads(row[5]),
             "retained_messages": json.loads(row[6]),
             "started_at": row[7], "finished_at": row[8]}
            for row in rows
        ]
        return items, items[-1]["sequence"] if has_more else None

    def delete(self, conversation_id: str) -> bool:
        """Delete only database conversation records, never workspace files or logs."""
        with closing(self.state._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _replace_messages(
        connection: sqlite3.Connection,
        conversation_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Replace a conversation snapshot while preserving caller-provided order."""
        connection.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,)
        )
        connection.executemany(
            "INSERT INTO conversation_messages(conversation_id, sequence, message_json) VALUES (?, ?, ?)",
            [
                (conversation_id, sequence, json.dumps(message, ensure_ascii=False, separators=(",", ":")))
                for sequence, message in enumerate(messages, start=1)
            ],
        )
