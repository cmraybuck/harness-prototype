"""Structured, revision-aware UTF-8 file changes."""

import os
import tempfile
import unittest
from pathlib import Path

from harness_prototype.files import apply_file_changes, create_directory, directory_creation_target, read_file


class FileChangeTests(unittest.TestCase):
    """Cover structured creation, updates, batch safety, and path handling."""

    def setUp(self) -> None:
        """Create and enter an isolated working directory for each test."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.original_directory = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.original_directory)

    def revision(self, path: Path) -> str:
        """Read the current revision through the model-facing interface."""
        result = read_file(str(path))
        self.assertTrue(result["ok"])
        return result["revision"]

    def test_create_directory_then_create_source_file(self) -> None:
        """Create the requested parent before writing a C program into it."""
        result = create_directory("TestProg")
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(result["path"], str(self.workspace / "TestProg"))
        write = apply_file_changes(".", [{"path": "TestProg/main.c", "action": "create",
                                          "content": 'int main(void) { return 0; }\n'}])
        self.assertTrue(write["ok"])
        self.assertEqual(Path.cwd(), self.workspace)
        self.assertFalse(create_directory(str(self.workspace / "TestProg"))["created"])

    def test_create_directory_rejects_invalid_targets_and_missing_parents(self) -> None:
        """Reject invalid inputs, file conflicts, and implicit recursive creation."""
        for path in (None, True, 42, "", "\x00"):
            with self.subTest(path=path):
                self.assertFalse(create_directory(path)["ok"])
        (self.workspace / "existing.txt").write_text("keep", encoding="utf-8")
        self.assertEqual(create_directory("existing.txt")["status"], "target_exists")
        self.assertEqual(create_directory("missing/child")["status"], "parent_missing")
        self.assertFalse((self.workspace / "missing").exists())
        self.assertEqual((self.workspace / "existing.txt").read_text(encoding="utf-8"), "keep")

    def test_directory_creation_access_targets_resolved_parent(self) -> None:
        """Authorize the folder's parent before any mutation."""
        self.assertEqual(directory_creation_target({"path": "TestProg"}), [self.workspace])
        self.assertFalse(directory_creation_target({"path": "TestProg", "extra": True})["ok"])

    def test_creates_multiple_utf8_file_types_in_one_batch(self) -> None:
        """Create source and documentation files in one structured operation."""
        result = apply_file_changes(".", [
            {"path": "notes.md", "action": "create", "content": "# Notes\n"},
            {"path": "main.py", "action": "create", "content": "print('hello')\n"},
            {"path": "sample.c", "action": "create", "content": "int main(void) {}\n"},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(
            (self.workspace / "notes.md").read_text(encoding="utf-8"),
            "# Notes\n",
        )
        self.assertEqual(
            (self.workspace / "main.py").read_text(encoding="utf-8"),
            "print('hello')\n",
        )
        self.assertEqual(
            (self.workspace / "sample.c").read_text(encoding="utf-8"),
            "int main(void) {}\n",
        )

    def test_append_inserts_line_separator_when_needed(self) -> None:
        """Append a text block without requiring the model to manage EOF newlines."""
        target = self.workspace / "poem.txt"
        target.write_text("last poem line", encoding="utf-8")
        result = apply_file_changes(".", [{
            "path": "poem.txt",
            "action": "append",
            "content": "I did it",
            "expected_revision": self.revision(target),
        }])
        self.assertTrue(result["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "last poem line\nI did it")

    def test_create_rejects_existing_target_without_modification(self) -> None:
        """Keep create distinct from update actions and preserve existing content."""
        target = self.workspace / "existing.md"
        target.write_text("original", encoding="utf-8")
        result = apply_file_changes(".", [
            {"path": "existing.md", "action": "create", "content": "replacement"},
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "target_exists")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_replace_requires_one_exact_match(self) -> None:
        """Replace one unique span and reject ambiguous source text."""
        target = self.workspace / "code.js"
        target.write_text("const value = 1;\n", encoding="utf-8")
        replaced = apply_file_changes(".", [{
            "path": "code.js",
            "action": "replace",
            "old_text": "value = 1",
            "content": "value = 2",
            "expected_revision": self.revision(target),
        }])
        self.assertTrue(replaced["ok"])
        target.write_text("same same", encoding="utf-8")
        ambiguous = apply_file_changes(".", [{
            "path": "code.js",
            "action": "replace",
            "old_text": "same",
            "content": "changed",
            "expected_revision": self.revision(target),
        }])
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["status"], "text_match_failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "same same")

    def test_overwrite_preserves_bom_and_crlf(self) -> None:
        """Preserve an existing file's UTF-8 BOM and detected newline style."""
        target = self.workspace / "windows.txt"
        target.write_bytes(b"\xef\xbb\xbfold\r\ntext\r\n")
        result = apply_file_changes(".", [{
            "path": "windows.txt",
            "action": "overwrite",
            "content": "new\ntext\n",
            "expected_revision": self.revision(target),
        }])
        self.assertTrue(result["ok"])
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfnew\r\ntext\r\n")

    def test_revision_mismatch_leaves_every_target_unchanged(self) -> None:
        """Preflight the complete batch before creating or updating any file."""
        target = self.workspace / "existing.txt"
        target.write_text("current", encoding="utf-8")
        result = apply_file_changes(".", [
            {"path": "new.txt", "action": "create", "content": "new"},
            {
                "path": "existing.txt",
                "action": "overwrite",
                "content": "changed",
                "expected_revision": "sha256:" + ("0" * 64),
            },
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "revision_mismatch")
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "current")

    def test_uses_explicit_base_path_outside_working_directory(self) -> None:
        """Resolve relative targets against a caller-selected authorized directory."""
        destination = self.workspace.parent / "destination"
        destination.mkdir()
        result = apply_file_changes(str(destination), [
            {"path": "outside.yaml", "action": "create", "content": "key: value\n"},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(
            (destination / "outside.yaml").read_text(encoding="utf-8"),
            "key: value\n",
        )
        self.assertFalse((self.workspace / "outside.yaml").exists())


if __name__ == "__main__":
    unittest.main()
