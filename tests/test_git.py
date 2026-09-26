"""Deterministic checks for bounded, read-only Git status inspection."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness_prototype.git import MAX_DIFF_CHARACTERS, MAX_STATUS_ENTRIES, git_diff, git_status


class GitStatusTests(unittest.TestCase):
    """Cover clean, changed, conflicted, bounded, and unavailable repositories."""

    def setUp(self) -> None:
        """Create a temporary repository and make it the active workspace."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.original_directory = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.original_directory)
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Harness Tests")
        self._git("config", "user.email", "harness@example.invalid")

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run one local-only Git setup command in the temporary repository."""
        return subprocess.run(
            ["git", *arguments], cwd=self.workspace, capture_output=True,
            check=check, text=True,
        )

    def _commit_file(self, path: str, content: str) -> None:
        """Create and commit one UTF-8 fixture file."""
        (self.workspace / path).write_text(content, encoding="utf-8")
        self._git("add", path)
        self._git("commit", "-m", f"add {path}")

    def test_clean_and_non_git_workspaces_are_explicit(self) -> None:
        """Distinguish a clean repository from a directory that is not a repository."""
        clean = git_status()
        self.assertTrue(clean["ok"])
        self.assertTrue(clean["clean"])
        self.assertEqual(clean["entry_count"], 0)

        with tempfile.TemporaryDirectory() as outside:
            os.chdir(outside)
            non_git = git_status()
        self.assertFalse(non_git["ok"])
        self.assertEqual(non_git["status"], "git_error")

    def test_categorizes_representative_worktree_and_index_states(self) -> None:
        """Report staged, unstaged, untracked, renamed, and deleted paths."""
        self._commit_file("rename me.txt", "original\n")
        self._commit_file("delete me.txt", "delete\n")
        self._commit_file("both.txt", "original\n")
        self._git("mv", "rename me.txt", "renamed file.txt")
        (self.workspace / "renamed file.txt").write_text("changed after rename\n", encoding="utf-8")
        (self.workspace / "delete me.txt").unlink()
        (self.workspace / "both.txt").write_text("staged\n", encoding="utf-8")
        self._git("add", "both.txt")
        with (self.workspace / "both.txt").open("a", encoding="utf-8") as output:
            output.write("unstaged\n")
        (self.workspace / "new file.txt").write_text("new\n", encoding="utf-8")

        result = git_status()

        self.assertTrue(result["ok"])
        self.assertFalse(result["clean"])
        self.assertIn("both.txt", result["staged"])
        self.assertIn("both.txt", result["unstaged"])
        self.assertIn("delete me.txt", result["deleted"])
        self.assertIn("new file.txt", result["untracked"])
        self.assertEqual(result["renamed"], [{
            "path": "renamed file.txt", "original_path": "rename me.txt",
            "index_status": "R", "worktree_status": "M",
        }])

    def test_reports_merge_conflicts_separately(self) -> None:
        """Categorize an unresolved merge without presenting it as ordinary staged work."""
        self._commit_file("conflict.txt", "base\n")
        self._git("switch", "-c", "topic")
        (self.workspace / "conflict.txt").write_text("topic\n", encoding="utf-8")
        self._git("commit", "-am", "topic change")
        self._git("switch", "main")
        (self.workspace / "conflict.txt").write_text("main\n", encoding="utf-8")
        self._git("commit", "-am", "main change")
        merge = self._git("merge", "topic", check=False)
        self.assertNotEqual(merge.returncode, 0)

        result = git_status()

        self.assertEqual(result["conflicted"], ["conflict.txt"])
        self.assertNotIn("conflict.txt", result["staged"])
        self.assertNotIn("conflict.txt", result["unstaged"])

    def test_bounds_large_results(self) -> None:
        """Stop parsing after the configured number of status entries."""
        for index in range(MAX_STATUS_ENTRIES + 1):
            (self.workspace / f"untracked-{index:04d}.txt").write_text("x", encoding="utf-8")
        result = git_status()
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["entry_count"], MAX_STATUS_ENTRIES)
        self.assertEqual(len(result["untracked"]), MAX_STATUS_ENTRIES)

    def test_nested_workspace_returns_only_workspace_relative_paths(self) -> None:
        """Scope a nested workspace without exposing changes elsewhere in its repository."""
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "inside.txt").write_text("inside", encoding="utf-8")
        (self.workspace / "outside.txt").write_text("outside", encoding="utf-8")
        os.chdir(nested)

        result = git_status()

        self.assertEqual(result["untracked"], ["inside.txt"])
        self.assertNotIn("outside.txt", result["untracked"])

    def test_missing_git_returns_a_structured_error(self) -> None:
        """Report an unavailable Git executable without raising an exception."""
        with patch("harness_prototype.git.subprocess.run", side_effect=FileNotFoundError):
            result = git_status()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "git_error")
        self.assertIn("not installed", result["error"])

    def test_diff_modes_report_distinct_patch_content(self) -> None:
        """Keep staged, unstaged, and combined patches scoped to their meanings."""
        self._commit_file("sample.txt", "base\n")
        (self.workspace / "sample.txt").write_text("staged\n", encoding="utf-8")
        self._git("add", "sample.txt")
        (self.workspace / "sample.txt").write_text("unstaged\n", encoding="utf-8")

        staged = git_diff("staged")
        unstaged = git_diff("unstaged")
        combined = git_diff("combined")

        self.assertIn("+staged", staged["diff"])
        self.assertNotIn("+unstaged", staged["diff"])
        self.assertIn("+unstaged", unstaged["diff"])
        self.assertIn("+unstaged", combined["diff"])
        self.assertNotIn("+staged", combined["diff"])
        self.assertTrue(combined["has_changes"])
        self.assertEqual(combined["file_count"], 1)
        self.assertEqual(combined["hunk_count"], 1)
        self.assertEqual(combined["additions"], 1)
        self.assertEqual(combined["deletions"], 1)
        self.assertEqual(combined["diff_characters"], len(combined["diff"]))
        self.assertEqual(combined["files"], [{
            "path": "sample.txt", "additions": 1, "deletions": 1,
            "hunk_count": 1,
        }])

    def test_diff_metadata_accounts_for_multiple_files_and_hunks(self) -> None:
        """Give the model structured evidence of the complete patch scope."""
        self._commit_file("first.txt", "one\ntwo\nthree\nfour\nfive\n")
        self._commit_file("second.txt", "old\n")
        (self.workspace / "first.txt").write_text(
            "changed one\ntwo\nthree\nfour\nchanged five\n", encoding="utf-8",
        )
        (self.workspace / "second.txt").write_text("new\n", encoding="utf-8")

        result = git_diff()

        self.assertTrue(result["has_changes"])
        self.assertEqual(result["file_count"], 2)
        self.assertGreaterEqual(result["hunk_count"], 2)
        self.assertEqual(result["additions"], 3)
        self.assertEqual(result["deletions"], 3)
        self.assertEqual(
            {item["path"] for item in result["files"]},
            {"first.txt", "second.txt"},
        )
        self.assertEqual(
            sum(item["hunk_count"] for item in result["files"]),
            result["hunk_count"],
        )

    def test_diff_filters_paths_and_reports_untracked_names(self) -> None:
        """Apply one safe filter to patches and untracked paths without reading contents."""
        self._commit_file("inside.txt", "old\n")
        self._commit_file("outside.txt", "old\n")
        (self.workspace / "inside.txt").write_text("inside change\n", encoding="utf-8")
        (self.workspace / "outside.txt").write_text("outside change\n", encoding="utf-8")
        (self.workspace / "untracked.txt").write_text("secret contents\n", encoding="utf-8")

        filtered = git_diff(path="inside.txt")
        all_changes = git_diff()

        self.assertIn("inside change", filtered["diff"])
        self.assertNotIn("outside change", filtered["diff"])
        self.assertEqual(filtered["untracked"], [])
        self.assertEqual(all_changes["untracked"], ["untracked.txt"])
        self.assertNotIn("secret contents", all_changes["diff"])
        self.assertFalse(all_changes["untracked_contents_included"])

    def test_diff_rejects_paths_outside_workspace(self) -> None:
        """Reject absolute and parent-relative path filters before diffing."""
        self.assertFalse(git_diff(path=str(self.workspace / "file.txt"))["ok"])
        self.assertFalse(git_diff(path="../file.txt")["ok"])

    def test_diff_rejects_unknown_mode(self) -> None:
        """Return a structured error for an unsupported diff scope."""
        result = git_diff("everything")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "git_error")

    def test_bounds_large_diff_output(self) -> None:
        """Return an explicit partial result when patch text reaches its bound."""
        self._commit_file("large.txt", "old\n")
        (self.workspace / "large.txt").write_text(
            "x" * (MAX_DIFF_CHARACTERS + 1) + "\n", encoding="utf-8",
        )
        result = git_diff("unstaged")
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["diff"]), MAX_DIFF_CHARACTERS)

    def test_diff_non_git_workspace_returns_structured_error(self) -> None:
        """Distinguish an invalid repository from a valid empty diff."""
        with tempfile.TemporaryDirectory() as outside:
            os.chdir(outside)
            result = git_diff()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "git_error")


if __name__ == "__main__":
    unittest.main()
