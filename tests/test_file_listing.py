"""Directory-listing checks that do not require a running model."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.files import list_files


class FileListingTests(unittest.TestCase):
    """Cover listing paths, validation, paging, and resource bounds."""

    def test_explicit_path_and_invalid_inputs(self) -> None:
        """List a requested directory without changing cwd and reject invalid paths."""
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.txt").write_text("sample", encoding="utf-8")
            self.assertEqual(list_files(path=temporary)["entries"], ["sample.txt"])
            self.assertFalse(list_files(path=str(root / "missing"))["ok"])
        self.assertEqual(Path.cwd(), original)
        self.assertFalse(list_files(path=True)["ok"])

    def test_stops_oversized_enumeration(self) -> None:
        """Reject oversized directories without consuming the remaining iterator."""
        entries = (Path(str(index)) for index in range(30_000))
        with patch(
            "harness_prototype.files.os.scandir",
            **{"return_value.__enter__.return_value": entries},
        ):
            self.assertFalse(list_files()["ok"])
        self.assertEqual(next(entries).name, "20001")

    def test_pages_and_output_cap(self) -> None:
        """Return every static entry across pages within the output budget."""
        entries = [Path(f"{index:04d}-" + "x" * 100) for index in range(250)]

        def list_page(offset: int = 0) -> dict:
            """List one deterministic page from the synthetic directory."""
            with patch(
                "harness_prototype.files.os.scandir",
                **{"return_value.__enter__.return_value": iter(entries)},
            ), patch("pathlib.Path.is_dir", return_value=False):
                return list_files(offset=offset)

        first = list_page()
        self.assertTrue(first["truncated"])
        self.assertLessEqual(len(json.dumps(first["entries"])), 4000)
        collected = list(first["entries"])
        offset = first["next_offset"]
        while offset is not None:
            page = list_page(offset)
            collected.extend(page["entries"])
            offset = page["next_offset"]
        self.assertEqual(collected, [path.name for path in entries])
        self.assertFalse(list_files(offset=-1)["ok"])
        self.assertFalse(list_files(max_entries=True)["ok"])

    def test_filters_exclusions_and_hidden_entries(self) -> None:
        """Apply filename, extension, exclusion, and hidden controls before paging."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in [".env", "alpha.py", "beta.PY", "notes.txt", "skip.py"]:
                (root / name).write_text(name, encoding="utf-8")
            (root / ".git").mkdir()
            (root / "build").mkdir()

            result = list_files(
                path=temporary,
                name_pattern="*.py",
                extensions=["py"],
                exclude_patterns=["skip.*"],
                include_hidden=False,
            )

        self.assertEqual(result["entries"], ["alpha.py", "beta.PY"])
        self.assertEqual(result["extensions"], ["py"])
        self.assertEqual(result["exclusions_applied"], ["skip.*"])
        self.assertFalse(result["include_hidden"])

    def test_default_exclusions_can_be_disabled(self) -> None:
        """Use repository-noise defaults only when exclusions are not overridden."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            (root / "build").mkdir()
            (root / "source").mkdir()

            defaults = list_files(path=temporary)
            unfiltered = list_files(path=temporary, exclude_patterns=[])

        self.assertEqual(defaults["entries"], ["source/"])
        self.assertIn(".git", defaults["exclusions_applied"])
        self.assertEqual(unfiltered["entries"], [".git/", "build/", "source/"])
        self.assertEqual(unfiltered["exclusions_applied"], [])

    def test_filtered_paging_and_invalid_patterns(self) -> None:
        """Page the filtered sequence and reject malformed filter values."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ["a.py", "b.txt", "c.py", "d.py"]:
                (root / name).write_text(name, encoding="utf-8")

            first = list_files(path=temporary, extensions=[".py"], max_entries=2)
            second = list_files(
                path=temporary,
                extensions=[".py"],
                max_entries=2,
                offset=first["next_offset"],
            )

        self.assertEqual(first["entries"], ["a.py", "c.py"])
        self.assertEqual(second["entries"], ["d.py"])
        self.assertFalse(list_files(name_pattern="[broken")["ok"])
        self.assertFalse(list_files(exclude_patterns="*.py")["ok"])
        self.assertFalse(list_files(exclude_patterns=["bad["])["ok"])
        self.assertFalse(list_files(extensions=["*.py"])["ok"])
        self.assertFalse(list_files(include_hidden=1)["ok"])


if __name__ == "__main__":
    unittest.main()
