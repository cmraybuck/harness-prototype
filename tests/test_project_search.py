"""Checks for bounded project-content search."""

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
                {
                    "path": str(self.root / "src" / "main.py"),
                    "line_number": 2,
                    "excerpt": "needle here",
                    "match_quality": "exact",
                },
                {
                    "path": str(self.root / "src" / "nested" / "other.py"),
                    "line_number": 1,
                    "excerpt": "another needle",
                    "match_quality": "exact",
                },
            ],
        )
        self.assertTrue(result["complete"])

    def test_complete_search_can_report_no_matches(self) -> None:
        """Distinguish an exhaustive empty result and offer bounded anchors."""
        result = search_project("absent message", "src")
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(result["match_quality"], "none")
        self.assertEqual(sum(result["match_counts"].values()), 0)
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["suggested_queries"], ["message", "absent"])

    def test_search_automatically_tolerates_case_and_whitespace_differences(self) -> None:
        """Find UI text without requiring the model to select a fallback mode."""
        prompt = self.root / "src" / "prompt.py"
        prompt.write_text('label = "You  > "\n', encoding="utf-8")

        result = search_project("you >", "src", "*.py")

        self.assertEqual(result["match_quality"], "whitespace_flexible")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(
            result["matches"],
            [{
                "path": str(prompt),
                "line_number": 1,
                "excerpt": 'label = "You  > "',
                "match_quality": "whitespace_flexible",
            }],
        )

    def test_search_ranks_fallbacks_without_hiding_weaker_candidates(self) -> None:
        """Rank stronger fallbacks first while retaining other useful candidates."""
        prompt = self.root / "src" / "prompt.py"
        prompt.write_text('first = "YOU >"\nsecond = "You  >"\n', encoding="utf-8")

        result = search_project("you >", "src")

        self.assertEqual(result["match_quality"], "mixed")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["matches"][0]["line_number"], 1)
        self.assertEqual(
            [match["match_quality"] for match in result["matches"]],
            ["case_insensitive", "whitespace_flexible"],
        )

    def test_search_ranks_exact_matches_without_hiding_fallback_candidates(self) -> None:
        """Keep an implementation candidate visible beside exact documentation text."""
        prompt = self.root / "src" / "prompt.py"
        prompt.write_text('first = "NEEDLE"\nsecond = "needle"\n', encoding="utf-8")

        result = search_project("needle", "src")

        self.assertEqual(result["match_quality"], "mixed")
        self.assertTrue(result["fallback_used"])
        prompt_matches = [
            match for match in result["matches"] if match["path"] == str(prompt)
        ]
        self.assertEqual(
            {match["match_quality"] for match in prompt_matches},
            {"exact", "case_insensitive"},
        )
        self.assertEqual(result["matches"][0]["match_quality"], "exact")

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

    def test_broad_search_excludes_generated_interaction_logs(self) -> None:
        """Prevent prior prompts and model output from outranking project source."""
        log_directory = self.root / "work" / "logs"
        log_directory.mkdir(parents=True)
        (log_directory / "session-example.jsonl").write_text(
            '{"user_prompt":"You >"}\n',
            encoding="utf-8",
        )
        source = self.root / "src" / "prompt.py"
        source.write_text('label = "You  > "\n', encoding="utf-8")

        result = search_project("You >")

        self.assertEqual(result["match_counts"]["exact"], 0)
        self.assertTrue(any(match["path"] == str(source) for match in result["matches"]))
        self.assertEqual(result["skipped_files"]["excluded"], 1)

    def test_explicit_log_search_remains_available(self) -> None:
        """Allow generated logs to be searched when the caller targets them directly."""
        log_directory = self.root / "work" / "logs"
        log_directory.mkdir(parents=True)
        log = log_directory / "session-example.jsonl"
        log.write_text('{"user_prompt":"You >"}\n', encoding="utf-8")

        result = search_project("You >", str(log_directory))

        self.assertEqual(result["matches"][0]["path"], str(log))

    def test_invalid_requests_return_errors(self) -> None:
        """Reject malformed and missing search inputs."""
        for arguments in (
            {}, {"text": ""}, {"text": "a\nb"},
            {"text": "x", "path": str(self.root / "missing")},
            {"text": "x", "file_pattern": "src/*.py"},
            {"text": "x", "max_results": True}, {"text": "x", "extra": 1},
        ):
            self.assertFalse(execute_tool("search_project", arguments)["ok"])

    def test_accepts_an_absolute_search_path(self) -> None:
        """Search an authorized outside path supplied as an absolute path."""
        result = search_project("needle", str(self.root / "src"), "*.py")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(self.root / "src"))

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
