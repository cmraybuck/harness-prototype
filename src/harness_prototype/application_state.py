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

CURRENT_SCHEMA_VERSION = 2
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
    except sqlite3.DatabaseError as error:
        raise CorruptStateStoreError(f"Invalid state database: {error}") from error
    if result != ("ok",) or table is None or version < (1 if allow_older else CURRENT_SCHEMA_VERSION):
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
