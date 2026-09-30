"""Check SQLite application-state migrations, transactions, and recovery."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from harness_prototype.application_state import (
    CURRENT_SCHEMA_VERSION,
    ConversationStore,
    CorruptStateStoreError,
    DurableGrantStore,
    StateStore,
    UnsupportedSchemaError,
)


class ApplicationStateTests(unittest.TestCase):
    """Cover durable application-state behavior with portable temporary databases."""

    def test_fresh_store_initializes_and_round_trips_json(self) -> None:
        """Initialize the current schema and preserve ordinary JSON values."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            store = StateStore(path)
            store.set("preferences", "editor", {"theme": "dark", "zoom": 1.25})
            self.assertEqual(
                store.get("preferences", "editor"),
                {"theme": "dark", "zoom": 1.25},
            )
            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    def test_version_one_fixture_migrates_without_losing_data(self) -> None:
        """Migrate a portable older fixture and retain its stored JSON."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
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
                connection.execute(
                    "INSERT INTO state_entries(scope, key, value_json) VALUES (?, ?, ?)",
                    ("portable", "fixture", '{"kept":true}'),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            store = StateStore(path)
            self.assertEqual(store.get("portable", "fixture"), {"kept": True})
            store.set("portable", "new", "value")

    def test_version_four_conversation_migrates_with_empty_provenance(self) -> None:
        """Add bounded source metadata while preserving legacy messages and selections."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            path = workspace / "state.sqlite3"
            state = StateStore(path)
            conversations = ConversationStore(state)
            conversations.create("legacy", "Legacy", "test-model", workspace,
                                 [{"role": "system", "content": "Retained context"}],
                                 active_skills=["pirate"])
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("ALTER TABLE conversations DROP COLUMN skill_provenance_json")
                connection.execute("PRAGMA user_version = 4")
                connection.commit()
            migrated = ConversationStore(StateStore(path)).load("legacy")
            self.assertEqual(migrated["active_skills"], ["pirate"])
            self.assertEqual(migrated["skill_provenance"], [])
            self.assertEqual(migrated["messages"][0]["content"], "Retained context")

    def test_failed_transaction_rolls_back_every_mutation(self) -> None:
        """Leave no partial records when a caller fails during a transaction."""
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            with self.assertRaisesRegex(ValueError, "valid JSON"):
                with store.transaction() as transaction:
                    transaction.set("test", "first", 1)
                    transaction.set("test", "invalid", object())
            self.assertIsNone(store.get("test", "first"))

    def test_concurrent_transactions_have_deterministic_result(self) -> None:
        """Serialize concurrent read-modify-write transactions without lost updates."""
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            store.set("counter", "value", 0)

            def increment(_: int) -> None:
                """Increment the shared fixture within one immediate transaction."""
                with store.transaction() as transaction:
                    transaction.set(
                        "counter",
                        "value",
                        transaction.get("counter", "value") + 1,
                    )

            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(increment, range(30)))
            self.assertEqual(store.get("counter", "value"), 30)

    def test_backup_restore_and_reset(self) -> None:
        """Create a consistent backup, restore it, and intentionally reset values."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.sqlite3")
            backup = root / "backups" / "state.sqlite3"
            store.set("preferences", "theme", "dark")
            self.assertEqual(store.backup_to(backup), backup)
            store.set("preferences", "theme", "light")
            store.restore_from(backup)
            self.assertEqual(store.get("preferences", "theme"), "dark")
            store.reset()
            self.assertIsNone(store.get("preferences", "theme"))

    def test_corrupt_and_newer_databases_are_rejected_without_replacement(self) -> None:
        """Reject unreadable and unsupported stores while preserving their bytes."""
        with tempfile.TemporaryDirectory() as temporary:
            corrupt = Path(temporary) / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a sqlite database")
            before = corrupt.read_bytes()
            with self.assertRaises(CorruptStateStoreError):
                StateStore(corrupt)
            self.assertEqual(corrupt.read_bytes(), before)

            newer = Path(temporary) / "newer.sqlite3"
            with closing(sqlite3.connect(newer)) as connection:
                connection.execute("PRAGMA user_version = 999")
                connection.commit()
            newer_before = newer.read_bytes()
            with self.assertRaises(UnsupportedSchemaError):
                StateStore(newer)
            self.assertEqual(newer.read_bytes(), newer_before)
            with closing(sqlite3.connect(newer)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_values_and_identifiers_are_bounded(self) -> None:
        """Reject oversized payloads and non-portable identifiers at the boundary."""
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            with self.assertRaisesRegex(ValueError, "65536"):
                store.set("scope", "key", "x" * 65536)
            for scope in ("", " padded", "line\nbreak"):
                with self.subTest(scope=scope), self.assertRaises(ValueError):
                    store.set(scope, "key", True)

    def test_durable_grants_match_a_narrow_scope_and_record_lifecycle(self) -> None:
        """Persist exact grants, update last use, and revoke them immediately."""
        with tempfile.TemporaryDirectory() as temporary:
            state = StateStore(Path(temporary) / "state.sqlite3")
            grants = DurableGrantStore(state)
            created = grants.create("grant-1", "executable", "C:/work", "git")

            self.assertIsNone(grants.match("executable", "C:/other", "git"))
            reused = DurableGrantStore(StateStore(state.path)).match(
                "executable", "C:/work", "git"
            )
            self.assertEqual(reused["grant_id"], "grant-1")
            self.assertGreaterEqual(reused["last_used_at"], created["last_used_at"])
            self.assertTrue(grants.revoke("grant-1"))
            self.assertIsNone(grants.match("executable", "C:/work", "git"))
            with closing(state._connect()) as connection:
                actions = [
                    row[0] for row in connection.execute(
                        "SELECT action FROM durable_grant_events ORDER BY sequence"
                    )
                ]
            self.assertEqual(actions, ["created", "reused", "revoked"])

    def test_conversation_turn_recovery_and_stable_paging(self) -> None:
        """Persist ordered messages and mark an abandoned turn interrupted on reopen."""
        with tempfile.TemporaryDirectory() as temporary:
            state = StateStore(Path(temporary) / "state.sqlite3")
            conversations = ConversationStore(state)
            workspace = Path(temporary)
            conversations.create(
                "first", "First", "model", workspace,
                [{"role": "system", "content": "one"}],
            )
            conversations.create(
                "second", "Second", "model", workspace,
                [{"role": "system", "content": "two"}],
            )
            page, cursor = conversations.list_conversations(1)
            self.assertEqual(len(page), 1)
            conversations.create(
                "third", "Third", "model", workspace,
                [{"role": "system", "content": "three"}],
            )
            following, _ = conversations.list_conversations(2, cursor)
            self.assertEqual([item["conversation_id"] for item in following], ["first"])

            conversations.start_turn("first", "turn", "inspect")
            conversations.save_turn_event(
                "first", "turn", {"sequence": 1, "type": "tool_result", "result": {"ok": True}}
            )
            reopened = ConversationStore(StateStore(state.path))
            history, next_cursor = reopened.history("first", 10)
            self.assertIsNone(next_cursor)
            self.assertEqual(history[0]["status"], "interrupted")
            self.assertEqual(history[0]["events"][0]["type"], "tool_result")
            self.assertEqual(
                reopened.load("first")["messages"],
                [{"role": "system", "content": "one"}],
            )


if __name__ == "__main__":
    unittest.main()
