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


if __name__ == "__main__":
    unittest.main()
