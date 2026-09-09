"""Filesystem and dispatch checks that do not require a running model."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.files import find_files
from harness_prototype.tools import execute_tool


class FileSearchTests(unittest.TestCase):
    def setUp(self):
        """Create a temporary nested folder tree for each search test."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "nested").mkdir()
        (self.root / "nested" / "Hello.txt").write_text("test")
        (self.root / "other.txt").write_text("test")

    def test_nested_case_insensitive_match(self):
        """Check that searching subfolders ignores filename capitalization."""
        result = find_files("hello.txt", str(self.root))
        self.assertEqual(result["matches"], [str(self.root / "nested" / "Hello.txt")])
        self.assertFalse(result["extension_inferred"])
        self.assertTrue(result["complete"])

    def test_extensionless_name_finds_same_named_file_with_extension(self):
        """Find a likely file when the user omits its extension."""
        result = find_files("hello", str(self.root))
        self.assertEqual(result["matches"], [str(self.root / "nested" / "Hello.txt")])
        self.assertTrue(result["extension_inferred"])
        self.assertTrue(result["complete"])

    def test_exact_extensionless_name_takes_precedence(self):
        """Prefer an exact extensionless file over inferred alternatives."""
        exact = self.root / "hello"
        exact.write_text("exact")
        result = find_files("hello", str(self.root))
        self.assertEqual(result["matches"], [str(exact)])
        self.assertFalse(result["extension_inferred"])

    def test_default_root(self):
        """Check that an omitted search root uses the current working directory."""
        original = Path.cwd()
        try:
            os.chdir(self.root)
            result = find_files("hello.txt")
            self.assertEqual(result["root"], str(self.root))
            self.assertEqual(len(result["matches"]), 1)
        finally:
            os.chdir(original)

    def test_result_limit_is_explicit(self):
        """Check that reaching the match limit reports a partial search."""
        result = find_files("*.txt", str(self.root), max_results=1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "result_limit")

    def test_missing_file_is_complete(self):
        """Check that a finished search can correctly return no matches."""
        result = find_files("missing.txt", str(self.root))
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["complete"])

    def test_bad_requests_return_errors(self):
        """Check that invalid search arguments and missing roots return errors."""
        for arguments in ({}, {"pattern": 1}, {"pattern": "../hello.txt"},
                          {"pattern": "*", "max_results": True},
                          {"pattern": "*", "extra": 1}):
            self.assertFalse(execute_tool("find_files", arguments)["ok"])
        self.assertFalse(find_files("*", str(self.root / "missing"))["ok"])

    def test_directory_link_is_not_followed(self):
        """Check that directory links are skipped instead of creating traversal cycles."""
        link = self.root / "cycle"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable on this Windows setup")
        result = find_files("hello.txt", str(self.root))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["skipped_links"], 1)
        self.assertFalse(result["complete"])

    def test_entry_limit(self):
        """Check that the entry budget stops traversal and reports incomplete results."""
        with patch("harness_prototype.files.MAX_ENTRIES", 0):
            result = find_files("*", str(self.root))
        self.assertEqual(result["stop_reason"], "entry_limit")
        self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
