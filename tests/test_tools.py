"""Registration and dispatch checks for model-facing tools."""

import unittest

from harness_prototype.tools import AVAILABLE_TOOLS, execute_tool


class ToolDispatchTests(unittest.TestCase):
    """Cover registry membership and dispatch-boundary validation."""

    def test_expected_tools_are_registered(self) -> None:
        """Expose exactly the supported read-only tool set."""
        self.assertEqual(
            set(AVAILABLE_TOOLS),
            {"get_location", "list_files", "find_files", "read_file"},
        )

    def test_rejects_unknown_tool_and_invalid_argument_shape(self) -> None:
        """Return structured errors for invalid dispatch requests."""
        self.assertFalse(execute_tool("unknown", {})["ok"])
        self.assertFalse(execute_tool("list_files", [])["ok"])

    def test_rejects_signature_mismatch(self) -> None:
        """Reject unexpected arguments before invoking a tool."""
        self.assertFalse(execute_tool("list_files", {"extra": True})["ok"])


if __name__ == "__main__":
    unittest.main()
