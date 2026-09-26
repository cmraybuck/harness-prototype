"""Deterministic checks for bounded, read-only Git status inspection."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness_prototype.git import MAX_STATUS_ENTRIES, git_status


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


if __name__ == "__main__":
    unittest.main()
