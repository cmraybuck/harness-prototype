"""Registration and dispatch checks for model-facing tools."""

import os
import tempfile
import unittest
from pathlib import Path

from harness_prototype.tools import AVAILABLE_TOOLS, execute_tool


class ToolDispatchTests(unittest.TestCase):
    """Cover registry membership and dispatch-boundary validation."""

    def test_expected_tools_are_registered(self) -> None:
        """Expose exactly the supported read-only tool set."""
        self.assertEqual(
            set(AVAILABLE_TOOLS),
            {"get_location", "list_files", "find_files", "search_project", "read_file"},
        )

    def test_rejects_unknown_tool_and_invalid_argument_shape(self) -> None:
        """Return structured errors for invalid dispatch requests."""
        self.assertFalse(execute_tool("unknown", {})["ok"])
        self.assertFalse(execute_tool("list_files", [])["ok"])

    def test_rejects_signature_mismatch(self) -> None:
        """Reject unexpected arguments before invoking a tool."""
        self.assertFalse(execute_tool("list_files", {"extra": True})["ok"])

    def assert_common_contract(self, result: dict, expected_ok: bool) -> None:
        """Check the status fields shared by every model-facing result."""
        self.assertEqual(result["ok"], expected_ok)
        self.assertIsInstance(result["complete"], bool)
        self.assertIsInstance(result["truncated"], bool)
        if expected_ok:
            self.assertIsNone(result["error"])
        else:
            self.assertIsInstance(result["error"], str)
            self.assertTrue(result["error"])
            self.assertFalse(result["complete"])
            self.assertFalse(result["truncated"])

    def test_all_registered_tools_follow_common_result_contract(self) -> None:
        """Require common status fields on successful and failed tool calls."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = root / "sample.txt"
            sample.write_text("needle\n", encoding="utf-8")
            original_directory = Path.cwd()
            try:
                os.chdir(root)
                successful_arguments = {
                    "get_location": {"location": "working_directory"},
                    "list_files": {"path": temporary},
                    "find_files": {"pattern": "sample.txt", "root": temporary},
                    "search_project": {"text": "needle"},
                    "read_file": {"path": str(sample)},
                }
                for name in AVAILABLE_TOOLS:
                    with self.subTest(name=name, outcome="success"):
                        self.assert_common_contract(
                            execute_tool(name, successful_arguments[name]),
                            expected_ok=True,
                        )
                    with self.subTest(name=name, outcome="failure"):
                        self.assert_common_contract(
                            execute_tool(name, {"unexpected": True}),
                            expected_ok=False,
                        )
            finally:
                os.chdir(original_directory)

    def test_partial_result_contract_distinguishes_truncation(self) -> None:
        """Mark a bounded page successful, incomplete, and truncated."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first.txt").write_text("first", encoding="utf-8")
            (root / "second.txt").write_text("second", encoding="utf-8")
            result = execute_tool(
                "list_files",
                {"path": temporary, "max_entries": 1},
            )
        self.assert_common_contract(result, expected_ok=True)
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 1)


if __name__ == "__main__":
    unittest.main()
