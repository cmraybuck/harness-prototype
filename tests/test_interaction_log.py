"""Persistent interaction-log format and privacy checks."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.interaction_log import InteractionLogger


class InteractionLoggerTests(unittest.TestCase):
    """Cover unique storage, ordered records, redaction, and write isolation."""

    def test_writes_valid_redacted_jsonl_without_overwriting(self) -> None:
        """Create unique session files and redact nested event content."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = InteractionLogger(directory, ("secret",))
            turn = first.start_turn("use secret")
            turn.add_event("tool_call", arguments={"token": "secret"})
            first.finish_turn(turn, "success")
            second = InteractionLogger(directory)

            files = list(directory.glob("*.jsonl"))
            self.assertEqual(len(files), 2)
            record = json.loads(first.path.read_text(encoding="utf-8"))
            self.assertEqual(record["session_id"], turn.session_id)
            self.assertEqual(record["turn_id"], turn.turn_id)
            self.assertEqual(record["events"][0]["sequence"], 1)
            self.assertNotIn("secret", json.dumps(record))
            self.assertIn("[REDACTED]", json.dumps(record))
            self.assertNotEqual(first.path, second.path)

    def test_write_failure_is_contained(self) -> None:
        """Disable later writes when persistence fails without raising to callers."""
        with tempfile.TemporaryDirectory() as temp:
            logger = InteractionLogger(Path(temp))
            turn = logger.start_turn("hello")
            with patch("pathlib.Path.open", side_effect=OSError("disk unavailable")):
                logger.finish_turn(turn, "success")
            self.assertIn("disk unavailable", logger.error)
            self.assertIsNone(logger.path)


if __name__ == "__main__":
    unittest.main()
