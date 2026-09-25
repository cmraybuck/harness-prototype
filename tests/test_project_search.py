"""Checks for bounded literal project-content search."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.files import search_project
from harness_prototype.tools import execute_tool


class ProjectSearchTests(unittest.TestCase):
    """Cover matches, exclusions, validation, and partial results."""

    def setUp(self) -> None:
        """Create and enter a disposable project tree for each test."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_directory = Path.cwd()
        self.addCleanup(os.chdir, self.original_directory)
        self.root = Path(self.temp.name).resolve()
        os.chdir(self.root)
        (self.root / "src" / "nested").mkdir(parents=True)
        (self.root / "src" / "main.py").write_text("first\nneedle here\n", encoding="utf-8")
        (self.root / "src" / "nested" / "other.py").write_text("another needle\n", encoding="utf-8")

    def test_finds_literal_text_in_nested_utf8_files(self) -> None:
        """Return relative paths, line numbers, and locating excerpts."""
        result = search_project("needle", "src", "*.py")
        self.assertEqual(
            result["matches"],
            [
                {"path": "src/main.py", "line_number": 2, "excerpt": "needle here"},
                {"path": "src/nested/other.py", "line_number": 1, "excerpt": "another needle"},
            ],
        )
        self.assertTrue(result["complete"])

    def test_complete_search_can_report_no_matches(self) -> None:
        """Distinguish an exhaustive empty result from a partial search."""
        result = search_project("absent", "src")
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["complete"])

    def test_excludes_dependencies_generated_git_and_binary_files(self) -> None:
        """Skip excluded directories and unsupported file contents."""
        for directory in (".git", "node_modules", "dist"):
            target = self.root / directory
            target.mkdir()
            (target / "hidden.txt").write_text("needle", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"needle\x00")
        result = search_project("needle")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["skipped_files"]["excluded"], 3)
        self.assertEqual(result["skipped_files"]["binary_or_non_utf8"], 1)

    def test_invalid_requests_return_errors(self) -> None:
        """Reject malformed, absolute, escaping, and missing search inputs."""
        for arguments in (
            {}, {"text": ""}, {"text": "a\nb"}, {"text": "x", "path": str(self.root)},
            {"text": "x", "path": "../outside"}, {"text": "x", "file_pattern": "src/*.py"},
            {"text": "x", "max_results": True}, {"text": "x", "extra": 1},
        ):
            self.assertFalse(execute_tool("search_project", arguments)["ok"])

    def test_result_and_entry_limits_are_explicit(self) -> None:
        """Mark results partial when a traversal or match budget stops work."""
        limited = search_project("needle", max_results=1)
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["stop_reason"], "result_limit")
        with patch("harness_prototype.files.MAX_ENTRIES", 0):
            limited = search_project("needle")
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["stop_reason"], "entry_limit")


if __name__ == "__main__":
    unittest.main()
